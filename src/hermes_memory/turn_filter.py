"""Turn-content filters shared by provider, extract_nouns, and backfill."""
from __future__ import annotations

import re

TURN_MIN_CHARS = 40

_NOISE_RE = re.compile(
    r"^("
    r"ok(ay)?|thanks?( you)?|thx|ty|np|"
    r"yes|no|sure|got it|done|cool|nice|great|"
    r"continue|please|exit|cancel|stop|quit|"
    r"yeah|yep|nope|alright"
    r")[\s\.\!\?]*$",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(r"(?<!\w)(?:i(?:['’]m)?|my|we)(?!\w)", re.IGNORECASE)


def _is_noise(
    content: str, *, min_chars: int = TURN_MIN_CHARS, role: str | None = None
) -> bool:
    return _noise_reason(content, min_chars=min_chars, role=role) is not None


def _noise_reason(
    content: str, *, min_chars: int = TURN_MIN_CHARS, role: str | None = None
) -> str | None:
    stripped = (content or "").strip()
    if not stripped:
        return "empty"
    if _NOISE_RE.match(stripped):
        return "noise_pattern"
    if len(stripped) >= min_chars:
        return None
    if role != "user":
        return "short_non_user"
    if _FIRST_PERSON_RE.search(stripped):
        return None
    from .reception import extract_assertions

    return "short_user_without_assertion" if not extract_assertions(stripped) else None
