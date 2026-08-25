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
