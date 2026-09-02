"""hermes-memory viz API — bind 127.0.0.1:7890.

Read-only surface the in-repo panes already call. Mutations return 501.
Vertex IDs are decimal strings at the JSON boundary (AGE bigint).
No extractors: graph comes from provider writes (Turn/Concept/ABOUT) plus
whatever ingest already MERGEd.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .store import Store, check_label, savepoint_name, validate_graph_name

logger = logging.getLogger("hermes_memory.graph_api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7890
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_LIMIT = 2000
DEFAULT_LIMIT = 250
PID_PATH = Path.home() / ".hermes" / "run" / "hermes-memory-api.pid"

RUNTIME: Optional["Runtime"] = None


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no DB)
# ---------------------------------------------------------------------------

def stringify_id(raw: Any) -> str:
    """AGE vertex ids must be decimal strings before they touch JavaScript."""
    if raw is None:
        raise ValueError("missing id")
    if isinstance(raw, bool):
        raise ValueError("invalid id")
    if isinstance(raw, dict):
        if "id" in raw:
            return stringify_id(raw["id"])
        raise ValueError("dict without id")
    s = str(raw).strip().strip('"')
    if s.endswith(".0") and s.replace(".", "", 1).isdigit():
        s = s[:-2]
    return str(int(s))


def _as_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    s = str(raw).strip()
    for suffix in ("::vertex", "::edge", "::path"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            s = s[1:-1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def parse_vertex(raw: Any) -> Optional[Dict[str, Any]]:
    obj = _as_json(raw)
    if not isinstance(obj, dict):
        return None
    try:
        vid = stringify_id(obj.get("id"))
    except (ValueError, TypeError):
        return None
    label = obj.get("label") or "Node"
    if isinstance(label, list):
        label = label[0] if label else "Node"
    raw_props = obj.get("properties")
    props: Dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    name = props.get("name") or props.get("path") or props.get("file_path") or vid
    return {
        "id": vid,
        "label": str(label),
        "name": str(name),
        "props": props,
    }


def parse_agtype_number(raw: Any, default: float = 0.5) -> float:
    if raw is None:
        return default
    try:
        s = str(raw).strip().strip('"')
        if s in ("", "null", "None"):
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def validate_bind_host(host: str) -> str:
    """Loopback only. 0.0.0.0 / wildcard / LAN addresses are rejected."""
    h = (host or "").strip().lower()
    if h in ("", "0.0.0.0", "::", "*", "[::]"):
        raise ValueError("graph api binds loopback only")
    if h not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing non-loopback bind {host!r}")
    if h == "localhost":
        return "127.0.0.1"
    return host


def human_turn_title(content: Any, fallback: str = "Turn") -> str:
    """First line of the message. Empty content keeps ``fallback`` (e.g. turn_id)."""
    text = str(content or "").strip()
    if not text:
        return fallback
    line = text.split("\n", 1)[0]
    line = re.sub(r"[#*_`]+", "", line).strip()
    if len(line) > 80:
        line = line[:77] + "…"
    return line or fallback


def humanize_node(n: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not n:
        return n
    props = n.get("props") or {}
    if n.get("label") == "Turn":
        title = human_turn_title(props.get("content"), n.get("name") or "Turn")
        n["name"] = title
        n["title"] = title
        n["snippet"] = str(props.get("content") or "")[:280]
    return n


SESSION_KINDS = frozenset({"interactive", "benchmark", "system_test"})


def classify_session_kind(session_id: Any, explicit: Any = None) -> str:
    """Closed enum for session intent. Prefixes classify legacy untagged rows."""
    if explicit in SESSION_KINDS:
        return str(explicit)
    s = str(session_id or "").strip().strip('"')
    if s.startswith("verify-c5") or s.startswith("c8-"):
        return "system_test"
    if s.startswith("bench-") or s.startswith("bench_"):
        return "benchmark"
    return "interactive"


def is_verify_session(session_id: Any) -> bool:
    """C5 verify synthetics use session_id verify-c5-verify-<ts>."""
    s = str(session_id or "").strip().strip('"')
    return s.startswith("verify-c5")


def is_synthetic_session(session_id: Any) -> bool:
    """Non-interactive sessions must not pollute recall or the Turn catalog."""
    return classify_session_kind(session_id) != "interactive"


def conversation_first_budget(limit: int) -> tuple[int, int]:
    """Split an 'all' view: conversations take 70%, remainder is File/IMPORTS."""
    n = max(1, int(limit))
    convo = max(1, (n * 7) // 10)
    other = max(0, n - convo)
    return convo, other


def clamp_limit(raw: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_LIMIT))


def safe_label(raw: Optional[str]) -> Optional[str]:
    """None means 'all labels'. Invalid labels raise ValueError."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "*", "all"):
        return None
    return check_label(s)


def parse_vertex_id_param(raw: str) -> int:
    return int(stringify_id(raw))


def preview_embedding(vec: Any, n: int = 64) -> Tuple[List[float], Dict[str, float]]:
    nums: List[float] = []
    if vec is None:
        return [], {"min": 0.0, "max": 0.0, "mean": 0.0}
    if isinstance(vec, (list, tuple)):
        nums = [float(x) for x in vec]
    else:
        s = str(vec).strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        if s:
            nums = [float(x) for x in s.split(",") if x.strip()]
    if not nums:
        return [], {"min": 0.0, "max": 0.0, "mean": 0.0}
    preview = nums[:n]
    return preview, {
        "min": round(min(nums), 6),
        "max": round(max(nums), 6),
        "mean": round(sum(nums) / len(nums), 6),
    }


def match_route(method: str, path: str) -> Tuple[str, Dict[str, str]]:
    """Return (route_name, path_params). Unknown → ('not_found', {})."""
    method = method.upper()
    path = path.rstrip("/") or "/"
    if method == "GET" and path in ("/api/health", "/health"):
        return "health", {}
    if method == "GET" and path in ("/api/librarian/health",):
        return "librarian_health", {}
    if method == "GET" and path in ("/api/librarian/graph/stats",):
        return "stats", {}
    if method == "GET" and path in ("/api/librarian/graph/3d", "/api/librarian/graph"):
        return "graph_3d", {}
    if method == "GET" and path in ("/api/librarian/chunks",):
        return "chunks", {}
    if method == "GET" and path in ("/api/librarian/search",):
        return "search", {}
    if method == "GET" and path in ("/api/librarian/verify",):
        return "verify", {}
    if method == "GET" and path in ("/", "/3d.html", "/fountain.html", "/api/librarian/pane"):
        return "pane", {}
    if method == "GET" and path in ("/index.html",):
        return "pane_index", {}
    if method == "POST" and path == "/api/librarian/nodes/merge":
        return "not_implemented", {}
    parts = path.split("/")
    if len(parts) == 5 and parts[1:4] == ["api", "librarian", "nodes"]:
        vid = parts[4]
        if method == "GET":
            return "node", {"vid": vid}
        if method in ("DELETE", "PATCH"):
            return "not_implemented", {"vid": vid}
    if len(parts) == 6 and parts[1:4] == ["api", "librarian", "nodes"] and parts[5] == "audit":
        if method == "GET":
            return "audit", {"vid": parts[4]}
    if method in ("POST", "PATCH", "DELETE", "PUT"):
        return "not_implemented", {}
    return "not_found", {}


def find_pane_dir() -> Optional[Path]:
    env = os.environ.get("HERMES_MEMORY_DOCS")
    if env:
        p = Path(env)
        if (p / "3d.html").is_file():
            return p
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "docs" / "graph",
        here.parents[1] / "docs" / "graph",
        Path.cwd() / "docs" / "graph",
    ]
    for cand in candidates:
        if (cand / "fountain.html").is_file() or (cand / "3d.html").is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# Async runtime (live 5450)
# ---------------------------------------------------------------------------

class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="graph-api-loop", daemon=False)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            return
        self._thread.join(timeout=5.0)
        if not self.loop.is_closed():
            try:
                self.loop.close()
            except Exception:
                logger.debug("loop close failed", exc_info=True)


def _load_dotenv_files() -> None:
    """Pull HYBRID_AGE_* from Hermes env files if missing from the process env.

    Never logs values.
    """
    homes = (
        Path.home() / ".hermes" / ".env",
        Path.home() / ".hermes" / "profiles" / "librarian" / ".env",
    )
    for path in homes:
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k.startswith("HYBRID_AGE_") or k == "HERMES_PG_PASSWORD":
                os.environ.setdefault(k, v)


class Runtime:
    def __init__(self) -> None:
        self.loop = _LoopThread()
        self.pool = None
        self.store: Optional[Store] = None
        self.embedder = None
        self.cfg = None
        _load_dotenv_files()
        self.loop.call(self._boot())

    async def _boot(self) -> None:
        import asyncpg

        from .config import load_config
        from .embed import Embedder

        self.cfg = load_config()
        dsn = self.cfg.dsn
        if "{pg_password}" in dsn:
            pw = os.environ.get("HERMES_PG_PASSWORD", "")
            dsn = dsn.replace("{pg_password}", pw)
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        self.store = Store(self.pool, graph_name=self.cfg.graph)
        try:
            self.embedder = Embedder(self.cfg.embed_url, self.cfg.embed_model, self.cfg.embed_dim)
        except Exception:
            logger.debug("embedder init failed", exc_info=True)
            self.embedder = None

    def close(self) -> None:
        async def _shutdown() -> None:
            if self.pool is not None:
                await self.pool.close()

        try:
            self.loop.call(_shutdown(), timeout=5)
        except Exception:
            logger.debug("pool close failed", exc_info=True)
        self.loop.stop()

    def health(self) -> Dict[str, Any]:
        return {"ok": True, "bind": f"{DEFAULT_HOST}:{DEFAULT_PORT}"}

    def librarian_health(self) -> Dict[str, Any]:
        assert self.store is not None
        counts = self.loop.call(self.store.librarian_health())
        healthy = counts.get("missing_hash", 1) == 0 and counts.get("missing_doc_type", 1) == 0
        return {"ok": healthy, **counts}

    def stats(self) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._astats())

    async def _astats(self) -> Dict[str, Any]:
        assert self.store is not None and self.pool is not None
        graph = self.store.graph_name
        health = await self.store.librarian_health()
        async with self.pool.acquire() as conn:
            await self.store.load_age(conn)
            verts = await self._count_cypher(conn, graph, "MATCH (n) RETURN count(n)")
            edges = await self._count_cypher(conn, graph, "MATCH ()-[r]->() RETURN count(r)")
            chunks = await conn.fetchval("SELECT count(*) FROM memory_entries")
            embedded = await conn.fetchval(
                "SELECT count(*) FROM memory_entries WHERE embedding IS NOT NULL"
            )
            bridge_rows = await conn.fetchval("SELECT count(*) FROM memory_chunk_nodes")
            bridge_verts = await conn.fetchval(
                "SELECT count(DISTINCT vertex_id) FROM memory_chunk_nodes"
            )
            orphan_chunks = await conn.fetchval(
                """
                SELECT count(*) FROM memory_entries e
                 WHERE metadata->>'file_path' IS NOT NULL
                   AND NOT EXISTS (
                     SELECT 1 FROM memory_chunk_nodes b
                      WHERE b.chunk_id = e.id::text
                   )
                """
            )
        drifted = int(health.get("missing_hash") or 0) + int(health.get("missing_doc_type") or 0)
        healthy = drifted == 0
        return {
            "graph": graph,
            "vertices": {"total": verts},
            "edges": {"total": edges},
            "vector": {
                "chunks": int(chunks or 0),
                "dim": int(getattr(self.cfg, "embed_dim", 768) or 768),
                "embedded": int(embedded or 0),
            },
            "bridge": {
                "rows": int(bridge_rows or 0),
                "vertices_bridged": int(bridge_verts or 0),
            },
            "integrity": {
                "healthy": healthy,
                "drifted": drifted,
                "orphan_chunks": int(orphan_chunks or 0),
                "isolated_files": 0,
                "orphan_deps": 0,
            },
        }

    async def _count_cypher(self, conn, graph: str, body: str) -> int:
        graph = validate_graph_name(graph)
        sp = savepoint_name("count", 0)
        async with conn.transaction():
            await conn.execute(f"SAVEPOINT {sp}")
            try:
                row = await conn.fetchrow(
                    f"SELECT * FROM cypher('{graph}', $$ {body} $$) AS (c agtype)"
                )
                await conn.execute(f"RELEASE SAVEPOINT {sp}")
            except Exception:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                logger.debug("count cypher failed: %s", body, exc_info=True)
                return 0
        if row is None:
            return 0
        try:
            return int(parse_agtype_number(row["c"], 0.0))
        except Exception:
            return 0

    def graph_3d(self, label: Optional[str], limit: int) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._agraph_3d(label, limit))

    async def _fetch_3d_rows(self, conn, graph: str, label: Optional[str], limit: int):
        if limit <= 0:
            return []
        if label:
            label_pat = f"(n:{label})"
        else:
            label_pat = "(n)"
        cypher = f"""
            MATCH {label_pat}
            OPTIONAL MATCH (n)-[r]->(m)
            RETURN n, type(r) AS rel, m, id(n), id(m),
                   coalesce(r.weight, 0.5), coalesce(r.cosine, 0.5)
            {("ORDER BY coalesce(n.turn_id, 0) DESC" if label == "Turn" else "")}
            LIMIT {int(limit)}
        """
        sp = savepoint_name("g3d", 0 if not label else 1)
        async with conn.transaction():
            await conn.execute(f"SAVEPOINT {sp}")
            try:
                rows = await conn.fetch(
                    f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS "
                    f"(n agtype, rel agtype, m agtype, nid agtype, mid agtype, w agtype, c agtype)"
                )
                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                return rows
            except Exception:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                logger.warning("graph/3d cypher failed label=%s", label, exc_info=True)
                return []

    def _assemble_3d(self, rows, limit: int, label: Optional[str]) -> Dict[str, Any]:
        nodes: Dict[str, Dict[str, Any]] = {}
        links: List[Dict[str, Any]] = []
        degree: Dict[str, int] = {}
        for row in rows:
            src = humanize_node(parse_vertex(row["n"]))
            if src and is_synthetic_session((src.get("props") or {}).get("session_id")):
                continue
            if src:
                nodes[src["id"]] = src
                degree[src["id"]] = degree.get(src["id"], 0)
            rel = row["rel"]
            rel_s = None if rel is None else str(rel).strip().strip('"')
            if rel_s in ("", "null", "None"):
                rel_s = None
            dst = humanize_node(parse_vertex(row["m"])) if row["m"] is not None else None
            if dst:
                nodes[dst["id"]] = dst
                degree[dst["id"]] = degree.get(dst["id"], 0)
            if src and dst and rel_s:
                links.append({
                    "source": src["id"],
                    "target": dst["id"],
                    "label": rel_s,
                    "weight": parse_agtype_number(row["w"]),
                    "cosine": parse_agtype_number(row["c"]),
                })
                degree[src["id"]] = degree.get(src["id"], 0) + 1
                degree[dst["id"]] = degree.get(dst["id"], 0) + 1
        out_nodes = []
        for vid, n in nodes.items():
            n = dict(n)
            n["val"] = 1 + degree.get(vid, 0)
            out_nodes.append(n)
        return {
            "nodes": out_nodes,
            "links": links,
            "meta": {
                "limit": limit,
                "label": label or "Turn+File",
                "verts": len(out_nodes),
                "edges": len(links),
            },
        }

    async def _agraph_3d(self, label: Optional[str], limit: int) -> Dict[str, Any]:
        assert self.store is not None and self.pool is not None
        graph = self.store.graph_name
        async with self.pool.acquire() as conn:
            await self.store.load_age(conn)
            if label:
                rows = await self._fetch_3d_rows(conn, graph, label, limit)
            else:
                convo_n, other_n = conversation_first_budget(limit)
                convo = await self._fetch_3d_rows(conn, graph, "Turn", convo_n)
                other = await self._fetch_3d_rows(conn, graph, "File", other_n)
                rows = list(convo) + list(other)
        return self._assemble_3d(rows, limit, label)

    def chunks(self, file_path: str, limit: int) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._achunks(file_path, limit))

    async def _achunks(self, file_path: str, limit: int) -> Dict[str, Any]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM memory_entries WHERE metadata->>'file_path' = $1",
                file_path,
            )
            rows = await conn.fetch(
                """
                SELECT id::text AS id, content, metadata, embedding::text AS embedding
                  FROM memory_entries
                 WHERE metadata->>'file_path' = $1
                 ORDER BY id
                 LIMIT $2
                """,
                file_path,
                limit,
            )
            bridged = await conn.fetchval(
                """
                SELECT count(DISTINCT vertex_id) FROM memory_chunk_nodes
                 WHERE chunk_id = ANY(
                    SELECT id::text FROM memory_entries WHERE metadata->>'file_path' = $1
                 )
                """,
                file_path,
            )
        chunks = []
        for r in rows:
            meta = r["metadata"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            meta = meta or {}
            preview, stats = preview_embedding(r["embedding"])
            content = r["content"] or ""
            chunks.append({
                "id": str(r["id"]),
                "doc_type": meta.get("doc_type"),
                "hash": meta.get("hash"),
                "language": meta.get("language"),
                "indexed_at": meta.get("indexed_at"),
                "preview": preview,
                "preview_stats": stats,
                "snippet": content[:400],
            })
        return {
            "chunks": chunks,
            "total": int(total or 0),
            "bridged_vertices": int(bridged or 0),
        }

    def search(self, q: str, k: int, hops: int) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._asearch(q, k, hops))

    async def _asearch(self, q: str, k: int, hops: int) -> Dict[str, Any]:
        from .embed import vec_to_literal

        assert self.store is not None
        t0 = time.perf_counter()
        vec = None
        if self.embedder is not None:
            vec = await self.embedder.embed_text(q[:800])
        t_embed = time.perf_counter()
        seeds = []
        if vec:
            seeds = await self.store.vector_search(vec_to_literal(vec), k)
        t_vec = time.perf_counter()
        vids = await self.store.bridge_vertex_ids([s["id"] for s in seeds] + [f"conv_{s['id']}" for s in seeds])
        int_ids = []
        for v in vids:
            try:
                int_ids.append(int(v))
            except (TypeError, ValueError):
                continue
        triples = []
        if int_ids and hops > 0:
            triples = await self.store.expand_graph(int_ids, limit=max(k * 4, 16))
        t_graph = time.perf_counter()
        results = []
        ranked = []
        by_label: Dict[str, int] = {}
        for s in seeds:
            results.append({
                "id": str(s["id"]),
                "content": s.get("content") or "",
                "score": float(s.get("similarity") or 0.0),
                "file_path": "",
            })
        for n, rel, m in triples:
            src = parse_vertex(n)
            dst = parse_vertex(m)
            node = dst or src
            if not node:
                continue
            by_label[node["label"]] = by_label.get(node["label"], 0) + 1
            ranked.append({
                "id": node["id"],
                "title": node["name"],
                "group": node["label"],
                "rank_score": 0.69,
                "governed": False,
            })
        if not ranked:
            for r in results:
                ranked.append({
                    "id": r["id"],
                    "title": (r["content"] or "")[:80] or r["id"],
                    "group": "memory",
                    "rank_score": r["score"],
                    "governed": False,
                })
        return {
            "query": q,
            "ranked": ranked[:k],
            "results": results,
            "retrieval": {
                "k": k,
                "hops": hops,
                "embed_model": getattr(self.cfg, "embed_model", "nomic-embed-text"),
                "embed_dim": getattr(self.cfg, "embed_dim", 768),
                "bridge_vertices": len(vids),
                "embed_ms": round((t_embed - t0) * 1000, 1),
                "vector_ms": round((t_vec - t_embed) * 1000, 1),
                "graph_ms": round((t_graph - t_vec) * 1000, 1),
                "fusion_ms": round((time.perf_counter() - t_graph) * 1000, 1),
                "by_label": by_label,
                "edges_traversed": len(triples),
                "graph_backend": "age",
            },
        }

    def node(self, vid: int) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._anode(vid))

    async def _anode(self, vid: int) -> Dict[str, Any]:
        assert self.store is not None and self.pool is not None
        graph = self.store.graph_name
        cypher = f"MATCH (n) WHERE id(n) = {int(vid)} RETURN n"
        async with self.pool.acquire() as conn:
            await self.store.load_age(conn)
            sp = savepoint_name("node", 0)
            async with conn.transaction():
                await conn.execute(f"SAVEPOINT {sp}")
                try:
                    row = await conn.fetchrow(
                        f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (n agtype)"
                    )
                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    logger.debug("node fetch failed", exc_info=True)
                    return {"error": "not found", "id": str(vid)}
        parsed = humanize_node(parse_vertex(row["n"]) if row else None)
        if not parsed:
            return {"error": "not found", "id": str(vid)}
        return {
            "id": parsed["id"],
            "label": parsed["label"],
            "name": parsed["name"],
            "title": parsed.get("title") or parsed["name"],
            "snippet": parsed.get("snippet") or "",
            "properties": parsed["props"],
        }

    def audit(self, vid: int) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._aaudit(vid))

    async def _aaudit(self, vid: int) -> Dict[str, Any]:
        assert self.store is not None and self.pool is not None
        graph = self.store.graph_name
        cypher = (
            f"MATCH (n) WHERE id(n) = {int(vid)} "
            f"OPTIONAL MATCH (n)-[r]-() RETURN count(r)"
        )
        async with self.pool.acquire() as conn:
            await self.store.load_age(conn)
            sp = savepoint_name("audit", 0)
            async with conn.transaction():
                await conn.execute(f"SAVEPOINT {sp}")
                try:
                    row = await conn.fetchrow(
                        f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (c agtype)"
                    )
                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    row = None
            bridge_rows = await conn.fetchval(
                "SELECT count(*) FROM memory_chunk_nodes WHERE vertex_id = $1",
                vid,
            )
        neighborhood = int(parse_agtype_number(row["c"], 0.0)) if row else 0
        return {
            "id": str(vid),
            "neighborhood": neighborhood,
            "edges": neighborhood,
            "bridge_rows": int(bridge_rows or 0),
            "bridged": int(bridge_rows or 0),
            "duplicates": 0,
        }

    def verify_readonly(self) -> Dict[str, Any]:
        """Read-only smoke for the pane badge. Does NOT run hermes-memory-verify
        (that writes synthetic turns)."""
        try:
            stats = self.stats()
        except Exception as exc:
            return {
                "status": "FAIL",
                "ok": False,
                "prefetch_len": 0,
                "graph_lines": 0,
                "summary": f"stats failed: {type(exc).__name__}",
            }
        verts = int(stats.get("vertices", {}).get("total") or 0)
        edges = int(stats.get("edges", {}).get("total") or 0)
        healthy = bool(stats.get("integrity", {}).get("healthy"))
        return {
            "status": "PASS" if healthy else "FAIL",
            "ok": healthy,
            "prefetch_len": verts,
            "graph_lines": edges,
            "len": verts,
            "chars": verts,
            "graph": edges,
            "summary": f"{verts}v/{edges}e healthy={healthy}",
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - " + format, self.address_string(), *args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "accept, content-type")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        route, params = match_route(self.command, parsed.path)
        rt = RUNTIME
        try:
            if route == "not_found":
                self._json(404, {"error": "not found"})
                return
            if route == "not_implemented":
                self._json(501, {"error": "not implemented", "detail": "read-only viz API"})
                return
            if route == "pane":
                self._serve_pane("fountain.html")
                return
            if route == "pane_index":
                self._serve_pane("index.html")
                return
            if rt is None:
                self._json(503, {"error": "runtime not ready"})
                return
            if route == "health":
                self._json(200, rt.health())
                return
            if route == "librarian_health":
                self._json(200, rt.librarian_health())
                return
            if route == "stats":
                self._json(200, rt.stats())
                return
            if route == "graph_3d":
                label = safe_label((qs.get("label") or [""])[0])
                limit = clamp_limit((qs.get("limit") or [DEFAULT_LIMIT])[0])
                self._json(200, rt.graph_3d(label, limit))
                return
            if route == "chunks":
                fp = (qs.get("file_path") or [""])[0]
                limit = clamp_limit((qs.get("limit") or [4])[0], default=4)
                self._json(200, rt.chunks(fp, limit))
                return
            if route == "search":
                q = (qs.get("q") or [""])[0]
                k = clamp_limit((qs.get("k") or [8])[0], default=8)
                hops = clamp_limit((qs.get("hops") or [2])[0], default=2)
                self._json(200, rt.search(q, k, hops))
                return
            if route == "verify":
                self._json(200, rt.verify_readonly())
                return
            if route == "node":
                vid = parse_vertex_id_param(params["vid"])
                self._json(200, rt.node(vid))
                return
            if route == "audit":
                vid = parse_vertex_id_param(params["vid"])
                self._json(200, rt.audit(vid))
                return
            self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            logger.exception("request failed %s %s", self.command, parsed.path)
            self._json(500, {"error": "internal error"})

    def _serve_pane(self, name: str) -> None:
        pane = find_pane_dir()
        if pane is None:
            self._json(404, {"error": "pane html not found in repo docs/graph"})
            return
        path = pane / name
        if not path.is_file():
            path = pane / "fountain.html"
        if not path.is_file():
            path = pane / "3d.html"
        body = path.read_bytes()
        self._bytes(200, body, "text/html; charset=utf-8")


def wait_ready(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 8.0) -> bool:
    import urllib.request

    url = f"http://{host}:{port}/api/librarian/graph/stats?fresh=1"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200 and len(resp.read()) > 20:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> Optional[int]:
    try:
        return int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def write_pid(pid: int) -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(pid) + "\n")


def stop_daemon() -> bool:
    pid = read_pid()
    if pid and pid_is_alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_is_alive(pid):
                break
            time.sleep(0.1)
        if pid_is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    # Only the serve listener — never pkill start/stop/status argv.
    try:
        import subprocess

        subprocess.run(["pkill", "-f", "hermes_memory.graph_api serve"], capture_output=True)
    except Exception:
        pass
    try:
        PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    global RUNTIME
    host = validate_bind_host(host)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    RUNTIME = Runtime()
    httpd = ThreadingHTTPServer((host, port), Handler)
    write_pid(os.getpid())
    logger.info("graph api listening on http://%s:%s", host, port)

    def _shutdown(*_args: Any) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if RUNTIME is not None:
            RUNTIME.close()
            RUNTIME = None
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def start_daemon(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Detach a listener. Used by hermes-memory-install after pip install -e."""
    host = validate_bind_host(host)
    if wait_ready(host, port, timeout=1.0):
        logger.info("graph api already up")
        return
    stop_daemon()
    log_path = Path("/tmp/librarian_api.log")
    cmd = [sys.executable, "-m", "hermes_memory.graph_api", "serve",
           "--host", host, "--port", str(port)]
    import subprocess

    proc = subprocess.Popen(
        cmd,
        stdout=open(log_path, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    write_pid(proc.pid)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-memory-api")
    sub = parser.add_subparsers(dest="cmd")
    serve_p = sub.add_parser("serve", help="foreground listener (default)")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    start_p = sub.add_parser("start", help="daemonize")
    start_p.add_argument("--host", default=DEFAULT_HOST)
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    cmd = args.cmd or "serve"
    if cmd == "serve":
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        try:
            host = validate_bind_host(host)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        serve(host, port)
        return 0
    if cmd == "start":
        try:
            host = validate_bind_host(args.host)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        start_daemon(host, args.port)
        if wait_ready(args.host, args.port, timeout=8.0):
            print(f"graph api up on http://{args.host}:{args.port}")
            return 0
        print("graph api failed to become ready; see /tmp/librarian_api.log", file=sys.stderr)
        return 1
    if cmd == "stop":
        stop_daemon()
        print("graph api stopped")
        return 0
    if cmd == "status":
        ready = wait_ready(timeout=1.5)
        pid = read_pid()
        print("up" if ready else "down", f"pid={pid or '-'}")
        return 0 if ready else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
