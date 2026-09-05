"""Safe AGE/Cypher string helpers (property-test surface P1/P2).

These live outside ``store.Store`` so ingest, loadgen, and tests can quote
Cypher without importing the data-access class.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

logger = logging.getLogger("hybrid_age.age_cypher")

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def age_str(value: Any) -> str:
    """Escape a Python value as a Cypher string literal.

    Guarantees no unescaped ``'`` or ``\\`` survives from the input.
    Non-string values are str()-ed first; None becomes 'null' (unquoted).
    Also escapes ``$`` (``\\$``) so the value can never close a
    dollar-quoted ``cypher('graph', $$ body $$)`` wrapper.
    """
    if value is None:
        return "null"
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace("$", "\\$")
    s = s.replace("'", "\\'")
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f"'{s}'"


def _pick_cypher_dollar_tag(body: str) -> str:
    """Pick a ``$tag$`` that does not collide with ``body``."""
    if "$$" not in body:
        return "$$"
    for i in range(10000):
        tag = f"$cy{i}$"
        if tag not in body:
            return tag
    raise ValueError("cypher body contains too many colliding dollar-quote tags")


def cypher_dollar_quote(body: str) -> str:
    """Wrap ``body`` in a safe dollar-quote tag."""
    tag = _pick_cypher_dollar_tag(body)
    return f"{tag}{body}{tag}"


def cypher_call(graph: str, body: str) -> str:
    """Return ``cypher('graph', $tag$body$tag$)`` with safe quoting."""
    validate_graph_name(graph)
    return f"cypher('{graph}', {cypher_dollar_quote(body)})"


_pick_dollar_tag = _pick_cypher_dollar_tag


def age_props(properties: Dict[str, Any]) -> str:
    """Render a dict as a Cypher property map."""
    parts = []
    for key, val in properties.items():
        if val is None:
            continue
        if not _SAFE_IDENT.match(key or ""):
            logger.warning("age_props: skipping invalid property key %r", key)
            continue
        if isinstance(val, bool):
            parts.append(f"{key}: {str(val).lower()}")
        elif isinstance(val, (int, float)):
            parts.append(f"{key}: {val}")
        else:
            parts.append(f"{key}: {age_str(val)}")
    return "{" + ", ".join(parts) + "}"


def check_label(label: str) -> str:
    """Validate a Cypher label against the safe-identifier pattern."""
    if not _SAFE_IDENT.match(label or ""):
        raise ValueError(f"invalid AGE label: {label!r}")
    return label


def validate_graph_name(graph: str) -> str:
    """Validate a graph name for safe interpolation into cypher('...', $$...$$)."""
    if not _SAFE_IDENT.match(graph or ""):
        raise ValueError(f"invalid AGE graph name: {graph!r}")
    return graph


_check_label = check_label


def savepoint_name(prefix: str, idx: int) -> str:
    """Build a SQL savepoint identifier (public API)."""
    return f"sp_{prefix}_{idx}"


_savepoint_name = savepoint_name
