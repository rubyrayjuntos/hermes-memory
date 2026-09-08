"""Pure viz-API helpers (no HTTP server / no live pool)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .age_cypher import check_label
from .walk import beam_score

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7890
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_LIMIT = 2000
DEFAULT_LIMIT = 250
GHOST_CONCEPT_LIMIT = 64
GHOST_MAX_K = 8
GHOST_MAX_LIMIT = 250

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
        score = float(row[6]) if row[6] is not None else None
        hop = int(row[7]) if row[7] is not None else 1
    elif nfields == 7:
        decay = float(row[5]) if row[5] is not None else 0.5
        score = float(row[6]) if row[6] is not None else None
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


def _stamp_graph_degree(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> None:
    degree: Dict[str, int] = {}
    for edge in edges:
        src = str(edge.get("source") or edge.get("from") or "")
        dst = str(edge.get("target") or edge.get("to") or "")
        if src:
            degree[src] = degree.get(src, 0) + 1
        if dst:
            degree[dst] = degree.get(dst, 0) + 1
    for node in nodes:
        node["val"] = 1 + degree.get(str(node.get("id") or ""), 0)


def assemble_catalog(
    *,
    age_nodes: List[Dict[str, Any]],
    age_links: List[Dict[str, Any]],
    passports: List[Dict[str, Any]],
    mentions: List[Dict[str, Any]],
    limit: int = 80,
) -> Dict[str, Any]:
    """Unscoped manifold: the same pack_search graph as /search, no query beam.

    Flower AGE rows are ignored. Session and turn stay on noun properties.
    """
    del age_nodes, age_links
    triples: List[Tuple[Any, ...]] = []
    for raw in mentions:
        src_noun = int(raw["src_noun"])
        tgt_noun = int(raw["tgt_noun"])
        src_name = str(raw.get("src_label") or raw.get("label") or src_noun)
        tgt_name = str(raw.get("tgt_label") or tgt_noun)
        mag = float(raw.get("magnitude") or 0.0)
        cosine = float(raw.get("cosine") or 0.0)
        decay = float(raw.get("decay") or 1.0)
        _c, _comp, score = beam_score(
            sim=0.0,
            src_align=0.0,
            tgt_align=0.0,
            prov_boost=0.0,
            decay=decay,
            magnitude=mag,
        )
        triples.append((
            {"id": str(src_noun), "name": src_name, "label": "Noun"},
            "mentions",
            {"id": str(tgt_noun), "name": tgt_name, "label": "Noun"},
            mag,
            cosine,
            decay,
            score,
            0,
        ))
    packed = pack_search("", 8, 0, [], [], triples)
    packed = attach_passport_anchors(packed, passports)
    graph = packed.get("graph") or {"nodes": [], "edges": []}
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or graph.get("links") or [])
    return {
        "nodes": nodes,
        "links": edges,
        "edges": edges,
        "graph": {"nodes": nodes, "edges": edges, "links": edges},
        "seeds": packed.get("seeds") or [],
        "paths": packed.get("paths") or [],
        "meta": {
            "limit": int(limit),
            "label": "Garden",
            "scope": "manifold",
            "verts": len(nodes),
            "edges": len(edges),
        },
    }


def attach_passport_anchors(
    packed: Dict[str, Any],
    passports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Stamp session/turn provenance onto search nouns. Do not inject flower gems."""
    graph = packed.setdefault("graph", {"nodes": [], "edges": [], "links": []})
    nodes: Dict[str, Dict[str, Any]] = {
        str(node["id"]): dict(node) for node in (graph.get("nodes") or [])
    }
    edges = list(graph.get("edges") or graph.get("links") or [])

    for passport in passports:
        noun_key = f"noun:{int(passport['noun_id'])}"
        vertex_id = passport.get("vertex_id")
        turn_hook = (
            f"age:{str(vertex_id).removeprefix('age:')}"
            if vertex_id is not None
            else None
        )
        if noun_key not in nodes:
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
        if turn_hook is not None:
            props["turn_vertex_id"] = turn_hook
        if passport.get("turn_id") is not None:
            props["turn_id"] = passport.get("turn_id")
        if passport.get("session_id") is not None:
            props["session_id"] = passport.get("session_id")
        if passport.get("type") is not None:
            props["type"] = passport.get("type")
        node["props"] = props
        if not node.get("name") and passport.get("label"):
            node["name"] = str(passport["label"])
            node["title"] = str(passport["label"])

    node_list = list(nodes.values())
    packed["graph"] = {"nodes": node_list, "edges": edges, "links": edges}
    _stamp_graph_degree(node_list, edges)
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
        if decay is not None:
            edges[-1]["decay"] = decay
        if score is not None:
            edges[-1]["score"] = score
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
            "rank_score": p["score"] if p.get("score") is not None else None,
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
    node_list = list(nodes.values())
    _stamp_graph_degree(node_list, edges)
    return {
        "query": q,
        "ranked": ranked[:k],
        "results": results,
        "seeds": seed_out,
        "graph": {"nodes": node_list, "edges": edges, "links": edges},
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


EMBED_UNSTAMPED = (
    "Embedding version not recorded (pre-V10) — trusted as nomic-768 by "
    "policy, not directly confirmed."
)
NO_CONNECTIONS = (
    "No connections yet — mentioned once, not yet linked to anything else."
)
TURN_UNAVAILABLE = (
    "Connected, but the source turn is unavailable (turn {turn_id} not found)."
)
TURN_GRAPH_DEGRADED = (
    "Extraction failed for this turn — this connection may be incomplete."
)
TURN_UNPASSPORTED = (
    "This turn predates full extraction and was never linked — see `RQ-PROD-2`."
)
SCORE_ONLY_IN_SEARCH = (
    "Score only available in the context of a search — run a query to see relevance."
)


def pack_retrieval_funnel(
    *,
    ann_candidates: int,
    above_similarity: int,
    min_similarity: float,
    seed_nodes: int,
    expanded_nodes: int,
    kept_after_beam: int,
) -> Dict[str, Any]:
    """Surface pipeline counts already computed — no new scoring."""
    after = int(seed_nodes) + int(expanded_nodes)
    line = (
        f"ANN candidates: {int(ann_candidates)}  →  "
        f"above similarity {min_similarity:g}: {int(above_similarity)}  →  "
        f"after graph expand: {after} ({int(seed_nodes)} seed + "
        f"{int(expanded_nodes)} new via edges)  →  "
        f"kept after beam/budget: {int(kept_after_beam)}"
    )
    return {
        "ann_candidates": int(ann_candidates),
        "above_similarity": int(above_similarity),
        "min_similarity": float(min_similarity),
        "after_graph_expand": after,
        "seed_nodes": int(seed_nodes),
        "expanded_nodes": int(expanded_nodes),
        "kept_after_beam": int(kept_after_beam),
        "line": line,
    }


def parse_noun_id_param(raw: str) -> int:
    s = str(raw).strip()
    if s.startswith("noun:"):
        s = s[5:]
    return int(stringify_id(s))


def pack_neighborhood(
    noun: Dict[str, Any],
    edges: List[Dict[str, Any]],
    turns: Dict[int, Dict[str, Any]],
    *,
    unpassported_ids: Optional[set[int]] = None,
) -> Dict[str, Any]:
    """1-hop mentions panel. Every empty path has a sourced reason."""
    unpassported_ids = unpassported_ids or set()
    embed_model = noun.get("embed_model")
    embed_dim = noun.get("embed_dim")
    if embed_model in (None, "") or embed_dim is None:
        embed_reason = EMBED_UNSTAMPED
    else:
        embed_reason = None
    neighbors: List[Dict[str, Any]] = []
    reasons: List[str] = []
    if not edges:
        reasons.append(NO_CONNECTIONS)
    for edge in edges:
        prov = [int(x) for x in (edge.get("provenance_turns") or [])]
        excerpts: List[Dict[str, Any]] = []
        for tid in prov:
            if tid in unpassported_ids:
                reasons.append(TURN_UNPASSPORTED)
            row = turns.get(tid)
            if row is None:
                reasons.append(TURN_UNAVAILABLE.format(turn_id=tid))
                continue
            status = row.get("drain_status")
            if status == "graph_degraded":
                reasons.append(TURN_GRAPH_DEGRADED)
            content = str(row.get("content") or "").strip()
            excerpts.append({
                "turn_id": tid,
                "excerpt": content[:240],
                "drain_status": status,
            })
        neighbors.append({
            "noun_id": int(edge["neighbor_id"]),
            "label": edge.get("neighbor_label"),
            "type": edge.get("neighbor_type"),
            "magnitude": edge.get("magnitude"),
            "provenance_turns": prov,
            "excerpts": excerpts,
        })
    # unique reasons, stable order
    seen: set[str] = set()
    uniq: List[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return {
        "id": f"noun:{int(noun['id'])}",
        "noun_id": int(noun["id"]),
        "label": noun.get("label"),
        "type": noun.get("type"),
        "embed_model": embed_model,
        "embed_dim": embed_dim,
        "embed_reason": embed_reason,
        "neighbors": neighbors,
        "empty_reasons": uniq,
    }


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
    if len(path.split("/")) == 6:
        hop_parts = path.split("/")
        if (
            hop_parts[1:4] == ["api", "librarian", "nouns"]
            and hop_parts[5] == "hop"
        ):
            if method == "GET":
                return "noun_hop", {"noun_id": hop_parts[4]}
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
