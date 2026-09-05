"""Live viz API async runtime (Postgres + AGE + search)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .graph_view import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    GHOST_CONCEPT_LIMIT,
    GHOST_MAX_K,
    GHOST_MAX_LIMIT,
    assemble_catalog,
    attach_passport_anchors,
    catalog_where_clause,
    humanize_node,
    pack_search,
    parse_agtype_number,
    parse_vertex,
    preview_embedding,
    stringify_id,
    undirected_knn_edges,
)
from .schema_guard import apply_pending_migrations
from .session_kind import is_synthetic_session
from .store import Store, clamp_hnsw_ef_search, cypher_call, savepoint_name, validate_graph_name
from .write_outcome import LEDGER

logger = logging.getLogger("hermes_memory.graph_api")

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
        if "***" in dsn:
            dsn = dsn.replace("***", os.environ.get("HERMES_PG_PASSWORD", ""))
        await asyncio.to_thread(apply_pending_migrations, dsn)
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        self.store = Store(
            self.pool,
            graph_name=self.cfg.graph,
            embed_model=self.cfg.embed_model,
            embed_dim=self.cfg.embed_dim,
            hnsw_ef_search=int(getattr(self.cfg, "hnsw_ef_search", 100)),
        )
        await self.store.require_schema_head()
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
        # LEDGER is process-local; snapshot ints reset when this process exits.
        return {"ok": True, "bind": f"{DEFAULT_HOST}:{DEFAULT_PORT}", **LEDGER.snapshot()}

    def librarian_health(self) -> Dict[str, Any]:
        assert self.store is not None
        counts = self.loop.call(self.store.librarian_health())
        healthy = counts.get("missing_hash", 1) == 0 and counts.get("missing_doc_type", 1) == 0
        return {"ok": healthy, **counts, **LEDGER.counts()}

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
            turns = await self._fetch_sql_count(conn, "SELECT count(*) FROM conversations")
            nouns = await self._fetch_sql_count(conn, "SELECT count(*) FROM noun")
            mentions = await self._fetch_sql_count(
                conn,
                "SELECT count(*) FROM semantic_edge WHERE verb_type = 'mentions'",
            )
            orphan_chunks = await conn.fetchval(
                """
                SELECT count(*) FROM memory_entries e
                 WHERE metadata->>'file_path' IS NOT NULL
                   AND NOT EXISTS (
                     SELECT 1 FROM memory_chunk_nodes b
                      WHERE b.chunk_id = e.id::text
                         OR b.chunk_id = 'mem_' || e.id::text
                   )
                """
            )
        drifted = int(health.get("missing_hash") or 0) + int(health.get("missing_doc_type") or 0)
        healthy = drifted == 0
        return {
            "graph": graph,
            "vertices": {"total": verts},
            "edges": {"total": edges},
            "manifold": {
                "turns": turns,
                "nouns": nouns,
                "mentions": mentions,
            },
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

    async def _fetch_sql_count(self, conn, query: str) -> int:
        try:
            return int(await conn.fetchval(query) or 0)
        except Exception:
            logger.debug("optional SQL count unavailable", exc_info=True)
            return 0

    async def _count_cypher(self, conn, graph: str, body: str) -> int:
        graph = validate_graph_name(graph)
        sp = savepoint_name("count", 0)
        async with conn.transaction():
            await conn.execute(f"SAVEPOINT {sp}")
            try:
                row = await conn.fetchrow(
                    f"SELECT * FROM {cypher_call(graph, body)} AS (c agtype)"
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
        where = catalog_where_clause(label)
        cypher = f"""
            MATCH {label_pat}
            {where}
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
                    f"SELECT * FROM {cypher_call(graph, cypher)} AS "
                    f"(n agtype, rel agtype, m agtype, nid agtype, mid agtype, w agtype, c agtype)"
                )
                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                return rows
            except Exception:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                logger.warning("graph/3d cypher failed label=%s", label, exc_info=True)
                return []

    async def _fetch_catalog_cypher(self, conn, graph: str, body: str, suffix: int):
        graph = validate_graph_name(graph)
        sp = savepoint_name("catalog", suffix)
        async with conn.transaction():
            await conn.execute(f"SAVEPOINT {sp}")
            try:
                rows = await conn.fetch(
                    f"SELECT * FROM {cypher_call(graph, body)} AS "
                    f"(n agtype, rel agtype, m agtype, w agtype, c agtype)"
                )
                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                return rows
            except Exception:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                logger.warning("Garden catalog Cypher failed", exc_info=True)
                return []

    async def _fetch_catalog_data(self, conn, graph: str, limit: int):
        """Load the full conversation mention manifold (same edges /search walks).

        ``limit`` is unused; kept so the Runtime call site stays stable.
        AGE flower is not fetched. ``graph`` is unused.
        """
        del graph, limit
        try:
            passport_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (p.noun_id)
                       p.noun_id, n.label, n.type, p.vertex_id, p.turn_id,
                       p.session_id
                  FROM memory_chunk_nodes p
                  JOIN noun n ON n.id = p.noun_id
                 WHERE p.source = 'conversation'
                   AND p.noun_id IS NOT NULL
                 ORDER BY p.noun_id, p.turn_id DESC
                """
            )
            passports = [dict(row) for row in passport_rows]
            mention_rows = await conn.fetch(
                """
                SELECT e.src_noun, e.tgt_noun,
                       src.label AS src_label, tgt.label AS tgt_label,
                       e.magnitude,
                       1 - (e.e_src_vec <=> e.e_tgt_vec) AS cosine,
                       exp(
                         -greatest(0, extract(epoch FROM (now() - last_active_ts)))
                         / 2592000.0
                       ) AS decay
                  FROM semantic_edge e
                  JOIN noun src ON src.id = e.src_noun
                  JOIN noun tgt ON tgt.id = e.tgt_noun
                 WHERE e.verb_type = 'mentions'
                """
            )
            mentions = [dict(row) for row in mention_rows]
        except Exception:
            logger.debug("Garden noun catalog unavailable", exc_info=True)
            passports, mentions = [], []
        return [], [], passports, mentions

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
                return self._assemble_3d(rows, limit, label)
            else:
                age_nodes, age_links, passports, mentions = await self._fetch_catalog_data(
                    conn, graph, int(limit),
                )
                return assemble_catalog(
                    age_nodes=age_nodes,
                    age_links=age_links,
                    passports=passports,
                    mentions=mentions,
                    limit=int(limit),
                )

    def ghost(self, k: int = 5, threshold: float = 0.70, limit: int = 200) -> Dict[str, Any]:
        assert self.store is not None
        return self.loop.call(self._aghost(k, threshold, limit))

    async def _aghost(self, k: int, threshold: float, limit: int) -> Dict[str, Any]:
        """Ghost kNN: render-time vector proximity, never persisted.
        For the visible Turn catalog (limit), fetch conversation embeddings and
        return up to k ghost edges per Turn above threshold. No AGE writes.
        Guarded by timeout(4) + bounded scoring + LRU cap.
        """
        assert self.store is not None and self.pool is not None
        k = max(1, min(int(k), GHOST_MAX_K))
        threshold = max(0.0, min(float(threshold), 0.99))
        limit = max(10, min(int(limit), GHOST_MAX_LIMIT))
        try:
            return await asyncio.wait_for(self._aghost_inner(k, threshold, limit), timeout=4.0)
        except asyncio.TimeoutError:
            logger.warning("ghost inner timeout after 4s k=%s threshold=%s limit=%s", k, threshold, limit)
            return {"ghost_edges": [], "ghost_links": [], "edges": [], "meta": {"k": k, "threshold": threshold, "visible": 0, "edges": 0, "timed_out": True}}

    async def _aghost_inner(self, k: int, threshold: float, limit: int) -> Dict[str, Any]:
        assert self.store is not None and self.pool is not None
        graph = self.store.graph_name
        async with self.pool.acquire() as conn:
            await self.store.load_age(conn)
            rows = await self._fetch_3d_rows(conn, graph, "Turn", limit)
        # Parse visible Turns -> turn_id + vid
        vids: List[str] = []
        turn_ids: List[int] = []
        vid_by_tid: Dict[int, str] = {}
        tid_by_vid: Dict[str, int] = {}
        for r in rows:
            parsed = parse_vertex(r["n"])
            if not parsed or parsed.get("label") != "Turn":
                continue
            props = parsed.get("props") or {}
            tid = props.get("turn_id")
            if tid is None:
                name = str(props.get("name") or parsed.get("name") or "")
                if name.startswith("turn_"):
                    try:
                        tid = int(name.split("_", 1)[1])
                    except Exception:
                        tid = None
            if tid is None:
                continue
            try:
                tid = int(tid)
            except Exception:
                continue
            vid = str(parsed["id"])
            vids.append(vid)
            turn_ids.append(tid)
            vid_by_tid[tid] = vid
            tid_by_vid[vid] = tid
        if not turn_ids:
            return {"nodes": [], "ghost_edges": [], "ghost_links": [], "meta": {"k": k, "threshold": threshold, "visible": 0, "edges": 0}}
        # Fetch embeddings for those turn_ids
        async with self.pool.acquire() as conn:
            crows = await conn.fetch(
                "SELECT id, embedding::text AS emb FROM conversations WHERE id = ANY($1::int[]) AND embedding IS NOT NULL",
                turn_ids,
            )
        emb_by_tid: Dict[int, List[float]] = {}
        for cr in crows:
            tid = int(cr["id"])
            txt = cr["emb"]
            if not txt:
                continue
            s = str(txt).strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            try:
                vec = [float(x) for x in s.split(",") if x.strip()]
            except Exception:
                continue
            if vec:
                emb_by_tid[tid] = vec
        # Cosine helper (Turn→Concept only; Turn↔Turn uses pgvector)
        def _cos(a: List[float], b: List[float]) -> float:
            if not a or not b or len(a) != len(b):
                return -1.0
            dot = 0.0
            na = 0.0
            nb = 0.0
            for x, y in zip(a, b):
                dot += x * y
                na += x * x
                nb += y * y
            if na == 0 or nb == 0:
                return -1.0
            return dot / (math.sqrt(na) * math.sqrt(nb))
        tids_with_emb = [tid for tid in turn_ids if tid in emb_by_tid]
        ghost_edges: List[Dict[str, Any]] = []
        # Turn↔Turn kNN in pgvector (C), then keep a pair if either side selected it
        neighbors: Dict[str, List[Tuple[float, str]]] = {}
        if tids_with_emb:
            try:
                async with self.pool.acquire() as conn:
                    pair_rows = await conn.fetch(
                        """
                        SELECT src_id, dst_id, cosine FROM (
                          SELECT a.id AS src_id,
                                 b.id AS dst_id,
                                 1 - (a.embedding <=> b.embedding) AS cosine,
                                 ROW_NUMBER() OVER (
                                   PARTITION BY a.id
                                   ORDER BY a.embedding <=> b.embedding
                                 ) AS rn
                            FROM conversations a
                            JOIN conversations b ON a.id <> b.id
                           WHERE a.id = ANY($1::int[])
                             AND b.id = ANY($1::int[])
                             AND a.embedding IS NOT NULL
                             AND b.embedding IS NOT NULL
                             AND 1 - (a.embedding <=> b.embedding) >= $2
                        ) ranked
                        WHERE rn <= $3
                        """,
                        tids_with_emb,
                        threshold,
                        k,
                    )
            except Exception:
                logger.debug("ghost knn query failed", exc_info=True)
                pair_rows = []
            for pr in pair_rows:
                src_vid = vid_by_tid.get(int(pr["src_id"]))
                dst_vid = vid_by_tid.get(int(pr["dst_id"]))
                if not src_vid or not dst_vid:
                    continue
                neighbors.setdefault(src_vid, []).append((float(pr["cosine"]), dst_vid))
            for src_vid, dst_vid, c in undirected_knn_edges(neighbors):
                ghost_edges.append({
                    "source": src_vid,
                    "target": dst_vid,
                    "from": src_vid,
                    "to": dst_vid,
                    "label": "GHOST_KNN",
                    "weight": 0.35,
                    "cosine": round(float(c), 4),
                })
        # --- Turn→Concept ghost bridging (runtime, never persisted) ---
        # Fetch Concept vertices (id + name) and embed names via embedder
        concept_vids: Dict[str, str] = {}  # name -> vid
        concept_rows: List[Any] = []
        concept_limit = max(1, min(GHOST_CONCEPT_LIMIT, int(limit)))
        try:
            async with self.pool.acquire() as conn:
                await self.store.load_age(conn)
                # Reuse SAVEPOINT pattern
                sp = "ghost_conc"
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        crows2 = await conn.fetch(
                            f"SELECT * FROM {cypher_call(graph, f'MATCH (c:Concept) RETURN id(c), c.name LIMIT {concept_limit} ')} AS (cid agtype, cname agtype)"
                        )
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        concept_rows = list(crows2)
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        concept_rows = []
        except Exception:
            concept_rows = []
        for cr in concept_rows:
            try:
                cid_raw = cr["cid"]
                cname_raw = cr["cname"]
                cid = str(cid_raw).strip().strip('"')
                # cid may be \"123\" string; normalize to int string
                try:
                    cid = str(int(float(cid.strip('"')))) if cid else ""
                except Exception:
                    cid = str(cid_raw).strip().strip('"')
                # Use same stringify helper
                try:
                    cid = stringify_id(cid_raw)
                except Exception:
                    pass
                cname = str(cname_raw).strip().strip('"') if cname_raw is not None else ""
                if not cname or cname in ("null", "None", ""):
                    continue
                # AGE returns json-encoded string "\"Hermes Agent\""
                try:
                    cname = json.loads(cname) if cname.startswith('"') else cname
                except Exception:
                    pass
                cname = str(cname).strip().strip('"')
                if cname:
                    concept_vids[cname] = cid
            except Exception:
                continue
        # Embed concept names (cache on Runtime); batch uncached names
        if concept_vids and self.embedder is not None:
            if not hasattr(self, "_ghost_concept_emb"):
                self._ghost_concept_emb: Dict[str, List[float]] = {}
            concept_emb: Dict[str, List[float]] = {}
            missing: List[str] = []
            for cname, cid in concept_vids.items():
                if not cname or not cid:
                    continue
                vec = self._ghost_concept_emb.get(cname)
                if vec is None:
                    missing.append(cname)
                else:
                    concept_emb[cname] = vec
            if missing:
                try:
                    batched = await asyncio.wait_for(self.embedder.embed_texts(missing), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("ghost concept embed timeout")
                    batched = [None] * len(missing)
                except Exception:
                    batched = [None] * len(missing)
                for cname, vec in zip(missing, batched):
                    if vec:
                        # LRU cap 128
                        if len(self._ghost_concept_emb) >= 128:
                            # evict oldest (first inserted)
                            oldest = next(iter(self._ghost_concept_emb))
                            del self._ghost_concept_emb[oldest]
                        self._ghost_concept_emb[cname] = list(vec)
                        concept_emb[cname] = list(vec)
            # For each Turn, score vs concepts, keep top 2 above lower threshold
            # Cap total pair scoring to 8000 to bound CPU (sample if larger)
            concept_thr = max(0.42, float(threshold) - 0.20)
            pairs_total = len(tids_with_emb) * len(concept_emb)
            if pairs_total > 8000:
                # sample concepts down to fit budget
                import random as _ghost_rng
                _ghost_rng.seed(0)
                max_concepts = max(1, 8000 // max(1, len(tids_with_emb)))
                sampled = _ghost_rng.sample(list(concept_emb.items()), min(len(concept_emb), max_concepts))
                concept_emb = dict(sampled)
            for tid in tids_with_emb:
                src_vid = vid_by_tid.get(tid)
                src_vec = emb_by_tid.get(tid)
                if not src_vid or not src_vec:
                    continue
                scored_c: List[Tuple[float, str]] = []
                for cname, cvec in concept_emb.items():
                    c = _cos(src_vec, cvec)
                    if c >= concept_thr:
                        scored_c.append((c, cname))
                scored_c.sort(key=lambda x: x[0], reverse=True)
                for c, cname in scored_c[:2]:
                    cid = concept_vids.get(cname)
                    if not cid:
                        continue
                    ghost_edges.append({
                        "source": src_vid,
                        "target": cid,
                        "from": src_vid,
                        "to": cid,
                        "label": "GHOST_KNN",
                        "weight": 0.55,
                        "cosine": round(float(c), 4),
                    })
        # Also dedupe exact duplicates (if both sides emitted)
        seen = set()
        uniq: List[Dict[str, Any]] = []
        for e in ghost_edges:
            key = (e["source"], e["target"])
            if key not in seen:
                seen.add(key)
                uniq.append(e)
        # split meta for UI
        turn_turn = sum(1 for e in uniq if e["target"] in tid_by_vid)
        turn_concept = len(uniq) - turn_turn
        return {
            "ghost_edges": uniq,
            "ghost_links": uniq,
            "edges": uniq,
            "meta": {"k": k, "threshold": threshold, "visible": len(tids_with_emb), "edges": len(uniq), "turn_turn": turn_turn, "turn_concept": turn_concept, "concepts": len(concept_vids), "limit": limit},
        }

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
                    OR chunk_id = ANY(
                    SELECT 'mem_' || id::text FROM memory_entries WHERE metadata->>'file_path' = $1
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
        from .walk import WalkHypothesis, WalkRow, parse_embedding

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
        conversation_seeds = [s for s in seeds if s.get("src") == "conversation"]
        conv_ids = [
            int(str(s["id"]).removeprefix("conv_"))
            for s in conversation_seeds
            if str(s.get("id") or "").removeprefix("conv_").isdigit()
        ]
        passports = await self.store.passports_for_conversations(conv_ids)
        seeds_by_chunk = {
            (
                str(s["id"])
                if str(s["id"]).startswith("conv_")
                else f"conv_{s['id']}"
            ): s
            for s in conversation_seeds
        }
        hypotheses = []
        for passport in passports:
            seed = seeds_by_chunk.get(str(passport.get("chunk_id") or ""))
            if seed is None:
                continue
            chunk_vec = parse_embedding(seed.get("embedding"))
            if not chunk_vec:
                continue
            hypotheses.append(
                WalkHypothesis(
                    noun_id=int(passport["noun_id"]),
                    chunk_id=str(passport["chunk_id"]),
                    session_id=str(passport.get("session_id") or ""),
                    turn_id=int(passport["turn_id"]),
                    sim=float(seed["similarity"]),
                    chunk_vec=chunk_vec,
                )
            )
        triples: List[Tuple[Any, ...]] = []
        if hypotheses and hops > 0:
            walk_rows = await self.store.expand_graph(
                hypotheses,
                q_vec=vec,
                hops=hops,
                k=k,
            )
            triples = [
                WalkRow(
                    tuple(row) + (int(row.audit["hop"]),),
                    audit=dict(row.audit),
                )
                for row in walk_rows
            ]
        t_graph = time.perf_counter()
        seed_noun_ids = list(
            dict.fromkeys(str(passport["noun_id"]) for passport in passports)
        )
        packed = pack_search(q, k, hops, seeds, seed_noun_ids, triples)
        packed = attach_passport_anchors(packed, passports)
        packed["retrieval"]["embed_model"] = getattr(self.cfg, "embed_model", "nomic-embed-text")
        packed["retrieval"]["embed_dim"] = getattr(self.cfg, "embed_dim", 768)
        packed["retrieval"]["hnsw_ef_search"] = clamp_hnsw_ef_search(
            getattr(self.store, "hnsw_ef_search", 100)
        )
        packed["retrieval"]["embed_ms"] = round((t_embed - t0) * 1000, 1)
        packed["retrieval"]["vector_ms"] = round((t_vec - t_embed) * 1000, 1)
        packed["retrieval"]["graph_ms"] = round((t_graph - t_vec) * 1000, 1)
        packed["retrieval"]["fusion_ms"] = round((time.perf_counter() - t_graph) * 1000, 1)
        return packed

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
                        f"SELECT * FROM {cypher_call(graph, cypher)} AS (n agtype)"
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
                        f"SELECT * FROM {cypher_call(graph, cypher)} AS (c agtype)"
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
