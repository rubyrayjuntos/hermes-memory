"""Closed session-kind enum — used by provider writes and the viz API."""
from __future__ import annotations

from typing import Any

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
