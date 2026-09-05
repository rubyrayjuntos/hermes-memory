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


def _is_noise(content: str, *, min_chars: int = TURN_MIN_CHARS) -> bool:
    stripped = (content or "").strip()
    if not stripped:
        return True
    if len(stripped) < min_chars:
        return True
    return bool(_NOISE_RE.match(stripped))
