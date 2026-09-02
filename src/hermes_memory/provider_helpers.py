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


def format_injection(seeds: list, max_paths: int = 3) -> str:
    """Bind graph routes onto seed headers so the LLM sees path + chunk together.

    Each seed: {score, content|excerpt, paths: [{triple, ...}]}.
    ``triple`` is the verify-countable ``[A] -REL-> [B]`` string.

    Do **not** wrap this in ``<memory-context>`` / ``</memory-context>``.
    Hermes ``sanitize_context`` deletes hyphen-tagged blocks before fencing.
    """
    if not seeds:
        return ""
    lines = [
        "Relevant memory context (hybrid vector + graph):",
    ]
    for i, s in enumerate(seeds, 1):
        pct = int(round(float(s.get("score") or s.get("similarity") or 0.0) * 100))
        excerpt = str(s.get("content") or s.get("excerpt") or "").replace("\n", " ").strip()[:220]
        paths = list(s.get("paths") or [])[:max_paths]
        if paths:
            route = " ; ".join(p.get("triple") or "" for p in paths if p.get("triple"))
            header = f"[SEED V{i} {pct}%] [Path: {route}]"
        else:
            header = f"[SEED V{i} {pct}%] [Path: none]"
        lines.append(header)
        if excerpt:
            lines.append(excerpt)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
