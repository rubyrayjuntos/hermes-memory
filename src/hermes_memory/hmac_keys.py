"""Per-install HMAC keys, shared by provider.py and extraction_worker.py
(kept in a leaf module with no dependents to avoid a provider <->
extraction_worker import cycle -- provider.py already imports
extraction_worker).

Same validation everywhere a keyed hash is needed: hex, >=32 bytes;
missing or invalid means no hash is computed at all, never a silent
downgrade to an unkeyed hash of what might be a short secret.

ROTATION (Spec 5 T5.7, documented 2026-11-21): rotating
HYBRID_AGE_WORK_EVENT_HMAC_KEY makes every content_hash already stored
under the old key permanently incomparable to a freshly-computed hash
under the new one -- nothing here re-hashes historical work_event or
work_artifact rows when the env var changes. A drift/identity check
spanning a rotation is expected to "mismatch" for a reason that has
nothing to do with the underlying content actually changing. This is
inherent to a keyed hash and is not a bug to fix; an operator rotating
the key should expect old hashes to go stale, same as rotating any
other secret invalidates prior HMACs computed with it.
"""
from __future__ import annotations

import os
from typing import Optional


def _read_hex_key(env_var: str) -> Optional[bytes]:
    secret_hex = os.environ.get(env_var, "")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        secret = b""
    if len(secret) < 32:
        return None
    return secret


def work_event_hmac_key() -> Optional[bytes]:
    """Spec 5 T5.1: the per-install key for work_event.content_hash."""
    return _read_hex_key("HYBRID_AGE_WORK_EVENT_HMAC_KEY")


def work_artifact_hmac_key() -> Optional[bytes]:
    """Spec 5 T5.5 (fix): the per-install key for work_artifact's file
    content_hash. Deliberately the SAME env var/key as
    work_event_hmac_key -- a repo root can hold a .env or other secret
    file, and a plain sha256 of a short secret has the identical
    brute-force problem content_hash was fixed for; reusing the same
    key means one rotation story for both, not two."""
    return _read_hex_key("HYBRID_AGE_WORK_EVENT_HMAC_KEY")
