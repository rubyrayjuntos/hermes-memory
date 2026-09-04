"""hermes-memory viz API — bind 127.0.0.1:7890.

Read-only surface the in-repo panes already call. Mutations return 501.
Vertex IDs are decimal strings at the JSON boundary (AGE bigint).
No extractors: graph comes from provider writes (Session/Turn/NEXT plus
Postgres noun passports and mentions) plus whatever ingest already MERGEd.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
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

from .store import Store, bridge_keys_for_seed, check_label, cypher_call, savepoint_name, validate_graph_name

logger = logging.getLogger("hermes_memory.graph_api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7890
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_LIMIT = 2000
DEFAULT_LIMIT = 250
GHOST_CONCEPT_LIMIT = 64
GHOST_MAX_K = 8
GHOST_MAX_LIMIT = 250
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
    name = (
        obj.get("name")
        or props.get("name")
        or props.get("path")
        or props.get("file_path")
        or vid
    )
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


def _allowed_cors_origin(origin: Optional[str]) -> Optional[str]:
    """Return the value to echo in ACAO, or None to omit the header.

    Only loopback origins (127.0.0.1 / localhost / ::1, any scheme/port)
    and the literal ``null`` (file:// sandbox) are allowed. Evil origins
    get no ACAO header — never ``*``. The caller reflects the exact
    ``Origin`` string so the allowlist cannot be probed via prefix tricks.
    """
    if not origin:
        return None
    o = origin.strip()
    if "\r" in o or "\n" in o:
        return None
    if o == "null":
        return "null"
    try:
        parsed = urlparse(o)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host in LOOPBACK_HOSTS and parsed.scheme in ("http", "https"):
        return o
    return None


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


def unpack_expand_row(
    row: Tuple[Any, ...],
) -> Tuple[Any, Any, Any, float, float, Optional[float], Optional[float], int]:
    """Parse expand_graph tuple plus optional hop suffix from /search.

    5-tuple: ``(n, rel, m, w, c)``
    6-tuple: ``(n, rel, m, w, c, hop)`` — legacy search packing
    7-tuple: ``(n, rel, m, w, c, decay, score)``
    8-tuple: ``(n, rel, m, w, c, decay, score, hop)``
    """
    n, rel, m = row[0], row[1], row[2]
    w = float(row[3]) if len(row) > 3 and row[3] is not None else 0.5
    c = float(row[4]) if len(row) > 4 and row[4] is not None else 0.5
    decay: Optional[float] = None
    score: Optional[float] = None
    hop = 1
    nfields = len(row)
    if nfields >= 8:
        decay = float(row[5]) if row[5] is not None else 0.5
        score = float(row[6]) if row[6] is not None else (0.5 * c + 0.3 * w + 0.2 * decay)
        hop = int(row[7]) if row[7] is not None else 1
    elif nfields == 7:
        decay = float(row[5]) if row[5] is not None else 0.5
        score = float(row[6]) if row[6] is not None else (0.5 * c + 0.3 * w + 0.2 * decay)
    elif nfields == 6:
        hop = int(row[5]) if row[5] is not None else 1
    return n, rel, m, w, c, decay, score, hop


def undirected_knn_edges(
    neighbors: Dict[str, List[Tuple[float, str]]],
) -> List[Tuple[str, str, float]]:
    """Canonical undirected edges from directed top-k neighbor lists.

    A pair is emitted if *either* endpoint selected the other. Dedup key is
    ``(min(src, dst), max(src, dst))``; cosine is the max of the two directions.
    """
    best: Dict[Tuple[str, str], float] = {}
    for src, scored in neighbors.items():
        for cosine, dst in scored:
            if not dst or dst == src:
                continue
            a, b = (src, dst) if src < dst else (dst, src)
            prev = best.get((a, b))
            c = float(cosine)
            if prev is None or c > prev:
                best[(a, b)] = c
    return [(a, b, c) for (a, b), c in best.items()]


def assemble_catalog(
    *,
    age_nodes: List[Dict[str, Any]],
    age_links: List[Dict[str, Any]],
    passports: List[Dict[str, Any]],
    mentions: List[Dict[str, Any]],
    limit: int = 80,
) -> Dict[str, Any]:
    """Assemble the Garden catalog from flower AGE rows and SQL nouns."""
    nodes: Dict[str, Dict[str, Any]] = {}
    links: List[Dict[str, Any]] = []
    degree: Dict[str, int] = {}

    for raw in age_nodes:
        if raw.get("label") not in {"Session", "Turn"}:
            continue
        node = dict(raw)
        node_id = f"age:{str(node['id']).removeprefix('age:')}"
        node["id"] = node_id
        nodes[node_id] = node
        degree[node_id] = 0

    for raw in age_links:
        if raw.get("label") not in {"NEXT", "IN_SESSION"}:
            continue
        source = f"age:{str(raw.get('source')).removeprefix('age:')}"
        target = f"age:{str(raw.get('target')).removeprefix('age:')}"
        if source not in nodes or target not in nodes:
            continue
        link = dict(raw)
        link.update({"source": source, "target": target})
        links.append(link)
        degree[source] += 1
        degree[target] += 1

    visible_nouns: set[int] = set()
    for passport in passports:
        noun_id = int(passport["noun_id"])
        node_id = f"noun:{noun_id}"
        turn_vertex_id = passport.get("vertex_id")
        turn_id = (
            f"age:{str(turn_vertex_id).removeprefix('age:')}"
            if turn_vertex_id is not None
            else None
        )
        props = {
            "type": passport.get("type"),
            "turn_id": passport.get("turn_id"),
            "turn_vertex_id": turn_id,
        }
        nodes[node_id] = {
            "id": node_id,
            "label": "Noun",
            "name": str(passport.get("label") or noun_id),
            "props": props,
        }
        degree.setdefault(node_id, 0)
        visible_nouns.add(noun_id)

    for raw in mentions:
        src_noun = int(raw["src_noun"])
        tgt_noun = int(raw["tgt_noun"])
        if src_noun not in visible_nouns or tgt_noun not in visible_nouns:
            continue
        source, target = f"noun:{src_noun}", f"noun:{tgt_noun}"
        links.append({
            "source": source,
            "target": target,
            "label": "mentions",
            "weight": float(raw.get("magnitude") or 0.0),
            "cosine": float(raw.get("cosine") or 0.0),
            "decay": float(raw.get("decay") or 1.0),
            "score": float(raw.get("score") or 0.0),
        })
        degree[source] += 1
        degree[target] += 1

    for node_id, node in nodes.items():
        node["val"] = 1 + degree.get(node_id, 0)
    return {
        "nodes": list(nodes.values()),
        "links": links,
        "meta": {
            "limit": int(limit),
            "label": "Garden",
            "verts": len(nodes),
            "edges": len(links),
        },
    }


def attach_passport_anchors(
    packed: Dict[str, Any],
    passports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Dock search nouns onto Turn vertices Fountain can snap to."""
    graph = packed.setdefault("graph", {"nodes": [], "edges": [], "links": []})
    nodes: Dict[str, Dict[str, Any]] = {
        str(node["id"]): dict(node) for node in (graph.get("nodes") or [])
    }
    edges = list(graph.get("edges") or graph.get("links") or [])

    for passport in passports:
        noun_key = f"noun:{int(passport['noun_id'])}"
        vertex_id = passport.get("vertex_id")
        turn_id = (
            f"age:{str(vertex_id).removeprefix('age:')}"
            if vertex_id is not None
            else None
        )
        if turn_id is not None and turn_id not in nodes:
            turn_name = f"turn {passport.get('turn_id') or turn_id}"
            nodes[turn_id] = {
                "id": turn_id,
                "label": "Turn",
                "group": "Turn",
                "name": turn_name,
                "title": turn_name,
                "props": {"turn_id": passport.get("turn_id")},
            }
        if noun_key not in nodes:
            if turn_id is None:
                continue
            noun_name = str(passport.get("label") or passport["noun_id"])
            nodes[noun_key] = {
                "id": noun_key,
                "label": "Noun",
                "group": "Noun",
                "name": noun_name,
                "title": noun_name,
                "props": {},
            }
        node = nodes[noun_key]
        props = dict(node.get("props") or node.get("properties") or {})
        if turn_id is not None:
            props["turn_vertex_id"] = turn_id
        if passport.get("turn_id") is not None:
            props["turn_id"] = passport.get("turn_id")
        if passport.get("type") is not None:
            props["type"] = passport.get("type")
        node["props"] = props
        if not node.get("name") and passport.get("label"):
            node["name"] = str(passport["label"])
            node["title"] = str(passport["label"])

    node_list = list(nodes.values())
    packed["graph"] = {"nodes": node_list, "edges": edges, "links": edges}
    packed.setdefault("retrieval", {})["vertices_reached"] = len(node_list)
    return packed


def pack_search(
    q: str,
    k: int,
    hops: int,
    seeds: List[Dict[str, Any]],
    seed_vertex_ids: List[str],
    triples: List[Tuple[Any, ...]],
) -> Dict[str, Any]:
    """Pure assembly of /search JSON: seeds, graph, paths, retrieval.

    triples: expand_graph 5- or 7-tuples, optionally with hop appended last.
    """
    seed_vertex_out = list(dict.fromkeys(
        f"noun:{str(v).removeprefix('noun:')}" for v in seed_vertex_ids
    ))
    seed_ids = set(seed_vertex_out)
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    paths: List[Dict[str, Any]] = []
    by_label: Dict[str, int] = {}

    def _ingest(raw: Any) -> Optional[Dict[str, Any]]:
        parsed = humanize_node(parse_vertex(raw))
        if not parsed:
            return None
        raw_id = str(parsed["id"])
        prefix = "noun" if parsed.get("label") == "Noun" else "age"
        vid = f"{prefix}:{raw_id.removeprefix(prefix + ':')}"
        parsed["id"] = vid
        nodes[vid] = parsed
        return parsed

    for row in triples:
        if not row or len(row) < 3:
            continue
        audit = getattr(row, "audit", {}) or {}
        n_raw, rel_raw, m_raw, w, c, decay, score, hop = unpack_expand_row(tuple(row))
        hop = int(audit.get("hop", hop))
        rel = None if rel_raw is None else str(rel_raw).strip().strip('"')
        if rel in ("", "null", "None"):
            rel = None
        raw_nodes = (parse_vertex(n_raw), parse_vertex(m_raw))
        if rel == "ABOUT" or any(
            node and node.get("label") == "Concept" for node in raw_nodes
        ):
            continue
        src = _ingest(n_raw)
        dst = _ingest(m_raw)
        if not src or not dst or not rel:
            continue
        edges.append({
            "source": src["id"],
            "target": dst["id"],
            "from": src["id"],
            "to": dst["id"],
            "label": rel,
            "weight": w,
            "cosine": c,
        })
        src_is_seed = src["id"] in seed_ids
        path: Dict[str, Any] = {
            "from": src["id"],
            "to": dst["id"],
            "rel": rel,
            "weight": w,
            "cosine": c,
            "hop": hop,
            "seed": src_is_seed,
            "from_label": src.get("label"),
            "to_label": dst.get("label"),
            "from_name": src.get("name") or src.get("title") or src["id"],
            "to_name": dst.get("name") or dst.get("title") or dst["id"],
        }
        if decay is not None:
            path["decay"] = decay
        if score is not None:
            path["score"] = score
        for key in ("session_id", "turn_id", "chunk_id"):
            if audit.get(key) is not None:
                path[key] = audit[key]
        paths.append(path)
        by_label[dst.get("label") or ""] = by_label.get(dst.get("label") or "", 0) + 1

    seed_out = []
    for s in seeds:
        excerpt = str(s.get("content") or "")[:160]
        raw_seed_id = str(s.get("id"))
        chunk_id = (
            raw_seed_id
            if raw_seed_id.startswith("conv_")
            else f"conv_{raw_seed_id}"
        )
        seed_id = (
            f"conv:{raw_seed_id.removeprefix('conv_').removeprefix('conv:')}"
            if s.get("src") == "conversation"
            else raw_seed_id
        )
        seed_out.append({
            "id": seed_id,
            "chunk_id": chunk_id if s.get("src") == "conversation" else raw_seed_id,
            "score": float(s.get("similarity") or s.get("score") or 0.0),
            "excerpt": excerpt,
            "vertex_ids": seed_vertex_out,
        })
    ranked = []
    for p in paths[:k]:
        ranked.append({
            "id": p["to"],
            "title": p["to_name"],
            "group": p["to_label"],
            "rank_score": (
                p["score"]
                if p.get("score") is not None
                else 0.5 * p["cosine"] + 0.3 * p["weight"]
            ),
            "governed": False,
        })
    if not ranked:
        for s in seed_out:
            ranked.append({
                "id": s["id"],
                "title": (s["excerpt"] or s["id"])[:80],
                "group": "memory",
                "rank_score": s["score"],
                "governed": False,
            })
    results = [{
        "id": (
            f"conv:{str(s.get('id')).removeprefix('conv_').removeprefix('conv:')}"
            if s.get("src") == "conversation"
            else str(s.get("id"))
        ),
        "chunk_id": (
            str(s.get("id"))
            if str(s.get("id")).startswith("conv_")
            else f"conv_{s.get('id')}"
        ) if s.get("src") == "conversation" else str(s.get("id")),
        "content": s.get("content") or "",
        "score": float(s.get("similarity") or 0.0),
        "file_path": "",
    } for s in seeds]
    return {
        "query": q,
        "ranked": ranked[:k],
        "results": results,
        "seeds": seed_out,
        "graph": {"nodes": list(nodes.values()), "edges": edges, "links": edges},
        "paths": paths,
        "retrieval": {
            "k": k,
            "hops": hops,
            "seeds": len(seed_out),
            "bridge_vertices": len(seed_vertex_ids),
            "vertices_reached": len(nodes),
            "edges_traversed": len(edges),
            "paths": len(paths),
            "by_label": by_label,
            "graph_backend": "age",
        },
    }


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


def catalog_where_clause(label: Optional[str]) -> str:
    """Push synthetic-session exclusion into Cypher so LIMIT is not starved.

    Only Turn vertices carry session_id. Prefixes match is_synthetic_session.
    Identifier is the static MATCH alias ``n`` — never user input.
    """
    if label != "Turn":
        return ""
    return (
        "WHERE NOT ("
        "coalesce(n.session_id, '') STARTS WITH 'bench-' OR "
        "coalesce(n.session_id, '') STARTS WITH 'bench_' OR "
        "coalesce(n.session_id, '') STARTS WITH 'verify-c5' OR "
        "coalesce(n.session_id, '') STARTS WITH 'c8-'"
        ")"
    )


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
    if method == "GET" and path in ("/api/librarian/graph/ghost",):
        return "ghost", {}
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
        """Fetch the latest Turn flower plus its SQL noun manifold."""
        turn_body = f"""
            MATCH (n:Turn)
            {catalog_where_clause("Turn")}
            RETURN n, null, null, 0.5, 0.5
            ORDER BY coalesce(n.turn_id, 0) DESC
            LIMIT {int(limit)}
        """
        turn_rows = await self._fetch_catalog_cypher(conn, graph, turn_body, 0)
        age_nodes: List[Dict[str, Any]] = []
        turn_vertex_ids: List[int] = []
        turn_ids: List[int] = []
        for row in turn_rows:
            node = humanize_node(parse_vertex(row["n"]))
            if not node:
                continue
            age_nodes.append(node)
            turn_vertex_ids.append(int(node["id"]))
            raw_turn_id = (node.get("props") or {}).get("turn_id")
            if raw_turn_id is not None and str(raw_turn_id).strip("-").isdigit():
                turn_ids.append(int(raw_turn_id))
        if not turn_vertex_ids:
            return age_nodes, [], [], []

        ids = ", ".join(str(vid) for vid in turn_vertex_ids)
        in_session_body = f"""
            MATCH (n:Turn)-[r:IN_SESSION]->(m:Session)
            WHERE id(n) IN [{ids}]
            RETURN n, type(r), m, coalesce(r.weight, 1.0), coalesce(r.cosine, 1.0)
        """
        next_body = f"""
            MATCH (n:Turn)-[r:NEXT]->(m:Turn)
            WHERE id(n) IN [{ids}] AND id(m) IN [{ids}]
            RETURN n, type(r), m, coalesce(r.weight, 1.0), coalesce(r.cosine, 1.0)
        """
        relation_rows = list(
            await self._fetch_catalog_cypher(conn, graph, in_session_body, 1)
        ) + list(await self._fetch_catalog_cypher(conn, graph, next_body, 2))
        age_links: List[Dict[str, Any]] = []
        known_age_ids = {node["id"] for node in age_nodes}
        for row in relation_rows:
            src = humanize_node(parse_vertex(row["n"]))
            dst = humanize_node(parse_vertex(row["m"]))
            rel = None if row["rel"] is None else str(row["rel"]).strip().strip('"')
            if not src or not dst or rel not in {"NEXT", "IN_SESSION"}:
                continue
            if dst["label"] == "Session" and dst["id"] not in known_age_ids:
                age_nodes.append(dst)
                known_age_ids.add(dst["id"])
            if src["id"] not in known_age_ids or dst["id"] not in known_age_ids:
                continue
            age_links.append({
                "source": src["id"],
                "target": dst["id"],
                "label": rel,
                "weight": parse_agtype_number(row["w"], 1.0),
                "cosine": parse_agtype_number(row["c"], 1.0),
            })

        try:
            passport_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (p.noun_id)
                       p.noun_id, n.label, n.type, p.vertex_id, p.turn_id
                  FROM memory_chunk_nodes p
                  JOIN noun n ON n.id = p.noun_id
                 WHERE p.source = 'conversation'
                   AND p.noun_id IS NOT NULL
                   AND p.turn_id = ANY($1::bigint[])
                 ORDER BY p.noun_id, p.turn_id DESC
                """,
                turn_ids,
            )
            passports = [dict(row) for row in passport_rows]
            noun_ids = [int(row["noun_id"]) for row in passports]
            mention_rows = await conn.fetch(
                """
                SELECT src_noun, tgt_noun, magnitude,
                       0.0::float AS cosine,
                       exp(
                         -greatest(0, extract(epoch FROM (now() - last_active_ts)))
                         / 2592000.0
                       ) AS decay,
                       (magnitude / 8.0) * exp(
                         -greatest(0, extract(epoch FROM (now() - last_active_ts)))
                         / 2592000.0
                       ) AS score
                  FROM semantic_edge
                 WHERE verb_type = 'mentions'
                   AND src_noun = ANY($1::int[])
                   AND tgt_noun = ANY($1::int[])
                """,
                noun_ids,
            ) if noun_ids else []
            mentions = [dict(row) for row in mention_rows]
        except Exception:
            logger.debug("Garden noun catalog unavailable", exc_info=True)
            passports, mentions = [], []
        return age_nodes, age_links, passports, mentions

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
                catalog_limit = min(int(limit), 80)
                age_nodes, age_links, passports, mentions = await self._fetch_catalog_data(
                    conn, graph, catalog_limit
                )
                return assemble_catalog(
                    age_nodes=age_nodes,
                    age_links=age_links,
                    passports=passports,
                    mentions=mentions,
                    limit=catalog_limit,
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

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - " + format, self.address_string(), *args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        allowed = _allowed_cors_origin(origin)
        if allowed is not None:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
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
            if route == "ghost":
                k = clamp_limit((qs.get("k") or [5])[0], default=5)
                k = max(1, min(k, GHOST_MAX_K))
                # threshold is 0..0.99 float, not a limit clamp
                try:
                    thr = float((qs.get("threshold") or ["0.70"])[0])
                except Exception:
                    thr = 0.70
                lim = clamp_limit((qs.get("limit") or [200])[0], default=200)
                lim = max(10, min(lim, GHOST_MAX_LIMIT))
                self._json(200, rt.ghost(k, thr, lim))
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
