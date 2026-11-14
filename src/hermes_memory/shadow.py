"""Shadow retrieval log: hashed query key, one row per prefetch, fire-and-forget.

Never stores the query text. Recording is scheduled after prefetch has its
string; a failing hook is logged and cannot change what prefetch returns.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from hashlib import sha256
from typing import Any, Callable

logger = logging.getLogger("hybrid_age")

KEY_BYTES = 16
_TASKS: set[asyncio.Task] = set()  # strong refs so pending shadow writes are not collected


def query_key(secret: bytes, text: str) -> bytes:
    """First 16 bytes of HMAC-SHA256 over the stripped query."""
    return hmac.new(secret, (text or "").strip().encode("utf-8"), sha256).digest()[:KEY_BYTES]


async def record_shadow(
    conn: Any, owner: str, key: bytes, vector_ids: list, hybrid_ids: list, latency_ms: dict
) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise ValueError(f"query key must be exactly {KEY_BYTES} bytes")
    await conn.execute(
        "INSERT INTO retrieval_shadow (owner, query_key, vector_ids, hybrid_ids, latency_ms) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb)",
        owner, bytes(key), json.dumps(list(vector_ids)), json.dumps(list(hybrid_ids)),
        json.dumps(dict(latency_ms)),
    )


async def _guarded(hook: Callable[..., Any], args: tuple) -> None:
    try:
        await hook(*args)
    except Exception:
        logger.warning("shadow hook failed", exc_info=True)


def schedule_shadow(hook: Callable[..., Any] | None, *args: Any) -> None:
    """Run `hook(*args)` as a background task. Returns immediately and never raises."""
    if hook is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(_guarded(hook, args))
    except Exception:
        logger.warning("shadow hook not scheduled", exc_info=True)
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
