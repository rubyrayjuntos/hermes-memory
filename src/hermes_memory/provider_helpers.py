"""hermes_memory.provider_helpers — agtype parsing/formatting for graph lines."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def parse_agtype_vertex(blob: Any) -> Optional[Dict]:
    """Parse an AGE vertex agtype into a Python dict."""
    if blob is None:
        return None
    raw = blob if isinstance(blob, str) else str(blob)
    for suffix in ("::vertex", "::edge", "::path"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def format_triple(n: Any, rel: Any, m: Any) -> str:
    def name(blob: Any) -> Optional[str]:
        data = parse_agtype_vertex(blob)
        if data is None:
            return None
        props = data.get("properties") or {}
        label = data.get("label") or ""
        nm = props.get("name") or props.get("summary") or ""
        if not nm:
            return None
        return f"{label} {nm}".strip() if label else str(nm)

    a, b = name(n), name(m)
    if not a:
        return ""
    rel_s = "" if rel is None else str(rel).strip('"')
    if b and rel_s and rel_s not in ("None", "null"):
        return f"[{a}] -{rel_s}-> [{b}]"
    return f"[{a}]"


def format_injection(seeds: list, max_paths: int = 3, meta: dict | None = None) -> str:
    """Bind graph routes onto seed headers so the LLM sees path + chunk together.

    Each seed: {score, content|excerpt, paths: [{triple, ...}]}.
    ``triple`` is the verify-countable ``[A] -REL-> [B]`` string.

    Structured wrapper ``<hybrid_injection>`` survives Hermes
    ``sanitize_context`` (which only strips hyphen ``<memory-context>``).
    Header exposes ranking components (score, w, c) and macro graph state.
    Per-seed decomposition ``score = 0.5*cos + 0.3*w + 0.2*rec`` is appended
    *after* the countable ``[SEED V1 64%] [Path: ... w= c=]`` so verify
    substring tests remain stable.
    """
    if not seeds:
        return ""
    # Header: structured, self-describing, survives sanitize (underscore tag)
    m = meta or {}
    seeds_n = int(m.get("seeds") or len(seeds))
    hops = int(m.get("hops") or 2)
    budget = m.get("budget")
    model = m.get("model") or "nomic-embed-text:768"
    thr = m.get("threshold")
    graph_line = m.get("graph_line")
    ghost_line = m.get("ghost_line")
    header_attrs = f'seeds={seeds_n} hops={hops} model="{model}"'
    if thr is not None:
        header_attrs += f' thr={thr}'
    if budget is not None:
        header_attrs += f' budget={budget}'
    lines = [
        f"<hybrid_injection {header_attrs}>",
    ]
    if graph_line or ghost_line:
        # macro state — single line, machine-parseable
        parts = []
        if graph_line:
            parts.append(graph_line)
        if ghost_line:
            parts.append(ghost_line)
        lines.append("[" + " | ".join(parts) + "]")
    for i, s in enumerate(seeds, 1):
        pct = int(round(float(s.get("score") or s.get("similarity") or 0.0) * 100))
        excerpt = str(s.get("content") or s.get("excerpt") or "").replace("\n", " ").strip()[:220]
        paths = list(s.get("paths") or [])[:max_paths]
        if paths:
            route = " ; ".join(p.get("triple") or "" for p in paths if p.get("triple"))
            header = f"[SEED V{i} {pct}%] [Path: {route}]"
        else:
            header = f"[SEED V{i} {pct}%] [Path: none]"
        # per-seed decomposition: composite = 0.5*c + 0.3*w + 0.2*rec
        # s may carry best_w/best_c/best_decay/best_score from _aprefetch; fall back to first path
        bw = s.get("best_weight")
        bc = s.get("best_cosine")
        br = s.get("best_decay")
        bs = s.get("best_score")
        if bs is None and paths:
            # first path carries w/c/decay/score when available
            p0 = paths[0]
            bw = bw if bw is not None else p0.get("weight")
            bc = bc if bc is not None else p0.get("cosine")
            br = br if br is not None else p0.get("decay")
            bs = bs if bs is not None else p0.get("score")
        if bs is not None and bc is not None and bw is not None and br is not None:
            try:
                header += f" score={float(bs):.2f} (0.5*{float(bc):.2f}+0.3*{float(bw):.2f}+0.2*{float(br):.2f})"
            except Exception:
                pass
        elif s.get("score") is not None:
            # vector-only seed: still expose cosine gate
            try:
                header += f" vec={float(s.get('score')):.2f}"
            except Exception:
                pass
        lines.append(header)
        if excerpt:
            lines.append(excerpt)
        lines.append("")
    lines.append("</hybrid_injection>")
    return "\n".join(lines).rstrip() + "\n"
