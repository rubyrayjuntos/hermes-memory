"""Background extraction worker (spec §5.2: a backlog, never on the prompt path).

`run_extraction_once` claims at most one `extract_rules` and one `extract_model`
job and settles them. The provider starts `extraction_loop` as its own task at
init. Prefetch never calls either; it only reads what the worker published.
Nothing here raises to the caller: failures are logged and the job is retried.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional, Sequence

from .extract_publish import publish_model, publish_rules
from .model_extract import grounded_claims

logger = logging.getLogger("hermes_memory.extraction")

RULES_LEASE_S = 60.0
MODEL_LEASE_S = 120.0
CONTEXT_TURNS = 4
CONTEXT_CHARS = 1000
IDLE_INTERVAL_S = 5.0

ModelFn = Callable[[str, Sequence[str]], Any]
EmbedFn = Callable[[Sequence[str]], Awaitable[list]]


async def _load_model_input(conn, span_id: int, owner: str) -> Optional[tuple[str, list[str], set[str]]]:
    span = await conn.fetchrow(
        "SELECT id, session_id, content FROM conversations WHERE id = $1 AND agent_identity = $2",
        int(span_id), owner)
    if span is None or not (span["content"] or "").strip():
        return None
    prev = await conn.fetch(
        "SELECT id, content FROM conversations WHERE agent_identity = $1 AND session_id = $2 "
        "AND id < $3 ORDER BY id DESC LIMIT $4",
        owner, span["session_id"], int(span_id), CONTEXT_TURNS)
    context = [(r["content"] or "")[:CONTEXT_CHARS] for r in reversed(prev)]
    verbs = {r["verb"] for r in await conn.fetch("SELECT verb FROM verb")}
    return span["content"], context, verbs


async def _settle_failure(store, job_id: int, fence: int, kind: str, exc: Exception) -> None:
    logger.warning("extraction %s job %s failed: %s", kind, job_id, exc, exc_info=True)
    try:
        await store.job_retry(job_id, fence, f"{type(exc).__name__}: {exc}"[:500])
    except Exception:
        logger.warning("extraction %s job %s retry settle failed", kind, job_id, exc_info=True)


async def _run_rules(store, worker: str, embed: EmbedFn) -> int:
    claimed = await store.job_claim("extract_rules", worker, 1, lease_s=RULES_LEASE_S,
                                    session_ordered=True, defer_backfill=True)
    if not claimed:
        return 0
    job_id, fence, _payload = claimed[0]
    try:
        async with store.pool.acquire() as conn:
            await publish_rules(conn, embed, job_id, fence)
    except Exception as exc:
        await _settle_failure(store, job_id, fence, "extract_rules", exc)
        return 0
    return 1


async def _run_model(store, worker: str, model: ModelFn) -> int:
    claimed = await store.job_claim("extract_model", worker, 1, lease_s=MODEL_LEASE_S,
                                    session_ordered=True, defer_backfill=True)
    if not claimed:
        return 0
    job_id, fence, payload = claimed[0]
    try:
        # Read everything the model needs, then release the connection: a slow
        # model must not pin a pool connection that prefetch also uses.
        async with store.pool.acquire() as conn:
            owner_row = await conn.fetchrow(
                "SELECT owner, payload FROM durable_job WHERE id = $1", int(job_id))
            loaded = await _load_model_input(conn, int(payload["span_id"]), owner_row["owner"])
        if loaded is None:
            # Span gone or empty: nothing to extract; settle so the queue moves on.
            return 1 if await store.job_done(job_id, fence) else 0
        turn, context, verbs = loaded
        raw = model(turn, context)
        if inspect.isawaitable(raw):
            raw = await raw
        claims = grounded_claims(raw, turn, context, verbs)
        async with store.pool.acquire() as conn:
            await publish_model(conn, claims, job_id, fence)
        # publish_model does not settle the job; a stale fence makes this False.
        return 1 if await store.job_done(job_id, fence) else 0
    except Exception as exc:
        await _settle_failure(store, job_id, fence, "extract_model", exc)
        return 0


async def run_extraction_once(provider, store, *, embed: EmbedFn, model: Optional[ModelFn]) -> int:
    """Claim and settle at most one rules job and one model job. Returns jobs finished.

    With `model=None` the model tier is skipped and its jobs stay queued.
    Never raises.
    """
    worker = str(getattr(provider, "_outbox_owner", "") or "extraction")
    finished = 0
    for step in (lambda: _run_rules(store, worker, embed),
                 (lambda: _run_model(store, worker, model)) if model is not None else None):
        if step is None:
            continue
        try:
            finished += await step()
        except Exception:
            logger.warning("extraction step failed", exc_info=True)
    return finished


async def extraction_loop(provider) -> None:
    """Poll for extraction work until cancelled. Runs as its own task, off the prompt path."""
    from . import extraction_worker  # resolve at call time so tests can wrap run_extraction_once

    while True:
        finished = 0
        try:
            store, embedder = provider.store, provider.embedder
            if store is not None and embedder is not None:
                finished = await extraction_worker.run_extraction_once(
                    provider, store, embed=embedder.embed_texts,
                    model=getattr(provider, "_extraction_model", None))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("extraction loop iteration failed", exc_info=True)
        await asyncio.sleep(0 if finished else IDLE_INTERVAL_S)
