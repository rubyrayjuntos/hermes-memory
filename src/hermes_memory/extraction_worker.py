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

from .extract_model_client import ModelBatchInput
from .extract_publish import publish_model, publish_rules
from .model_extract import grounded_claims
from .versions import WORK_VERSION

logger = logging.getLogger("hermes_memory.extraction")

RULES_LEASE_S = 60.0
MODEL_LEASE_S = 120.0
WORK_LEASE_S = 120.0
IDLE_INTERVAL_S = 5.0

ModelFn = Callable[[ModelBatchInput], Any]
EmbedFn = Callable[[Sequence[str]], Awaitable[list]]


async def _load_model_batch_input(conn, span_ids: Sequence[int], owner: str, *,
                                  allow_assistant: bool = False) -> Optional[ModelBatchInput]:
    from .reception import extract_assertions, extract_self_assertions
    from .turn_filter import segment_turn, strip_memory_blocks

    allowed_roles = ("user", "assistant") if allow_assistant else ("user",)
    turns = []
    rule_findings = []
    for span_id in span_ids:
        span = await conn.fetchrow(
            "SELECT id, session_id, content, role FROM conversations "
            "WHERE id = $1 AND agent_identity = $2",
            int(span_id), owner,
        )
        if span is None or span["role"] not in allowed_roles or not (span["content"] or "").strip():
            continue
        role = str(span["role"])
        # T4.3: the stripper runs before anything else touches the text --
        # an injected <memory>/<PAST_CONTEXT>/<grounded_facts>/
        # <supporting_turns> block must never feed back into extraction
        # (would otherwise let the system confirm its own injected memory).
        text = strip_memory_blocks(str(span["content"]))
        authored = "".join(
            text[seg.start:seg.end] for seg in segment_turn(text) if seg.kind == "authored"
        )
        if not authored.strip():
            continue
        previous = await conn.fetchrow(
            "SELECT content FROM conversations "
            "WHERE session_id = $1 AND agent_identity = $2 AND id < $3 "
            "ORDER BY id DESC LIMIT 1",
            span["session_id"], owner, int(span_id),
        )
        turns.append({
            "span_id": int(span_id),
            "text": authored,
            "context": str(previous["content"]) if previous and previous["content"] else "",
            "speaker": role,
        })
        if role == "user":
            # Site 1 invariant: rules stay user-only, including the
            # rule_findings hints passed alongside the model prompt.
            rule_findings.extend(
                {"span_id": int(span_id), **claim} for claim in extract_assertions(authored)
            )
            # SPEC2: extract_self_assertions after extract_assertions (authored segments only)
            rule_findings.extend(
                {"span_id": int(span_id), **claim} for claim in extract_self_assertions(authored)
            )
    if not turns:
        return None
    verbs = {r["verb"] for r in await conn.fetch("SELECT verb FROM verb")}
    return ModelBatchInput(turns=turns, rule_findings=rule_findings, verbs=verbs)


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


async def _run_model(store, worker: str, model: ModelFn, embed: EmbedFn, *,
                     allow_assistant: bool = False) -> int:
    claimed = await store.job_claim("extract_model", worker, 1, lease_s=MODEL_LEASE_S,
                                    session_ordered=True, defer_backfill=True,
                                    ready_model_batch=True)
    if not claimed:
        return 0
    job_id, fence, payload = claimed[0]
    try:
        # Read everything the model needs, then release the connection: a slow
        # model must not pin a pool connection that prefetch also uses.
        async with store.pool.acquire() as conn:
            owner_row = await conn.fetchrow(
                "SELECT owner, payload FROM durable_job WHERE id = $1", int(job_id)
            )
            span_ids = payload.get("span_ids", [payload.get("span_id")])
            loaded = await _load_model_batch_input(
                conn, [int(span_id) for span_id in span_ids if span_id is not None],
                owner_row["owner"], allow_assistant=allow_assistant,
            )
        if loaded is None:
            return 1 if await store.job_done(job_id, fence) else 0
        record_call = getattr(store, "job_record_model_call", None)
        if callable(record_call) and not await record_call(job_id, fence):
            return 0
        raw = model(loaded)
        if inspect.isawaitable(raw):
            raw = await raw
        turn_text_by_id = {int(turn["span_id"]): str(turn["text"]) for turn in loaded.turns}
        turn_speaker_by_id = {
            int(turn["span_id"]): str(turn.get("speaker") or "user") for turn in loaded.turns
        }
        claims = []
        raw_claims = raw.get("claims", []) if isinstance(raw, dict) else []
        for candidate in raw_claims if isinstance(raw_claims, list) else []:
            if not isinstance(candidate, dict):
                continue
            span_id = candidate.get("span_id")
            if not isinstance(span_id, int) or isinstance(span_id, bool):
                continue
            turn = turn_text_by_id.get(span_id)
            if turn is None:
                continue
            speaker = turn_speaker_by_id.get(span_id, "user")
            accepted = grounded_claims(
                {"claims": [candidate]}, turn, [], loaded.verbs, speaker=speaker,
            )
            claims.extend({**claim, "span_id": span_id} for claim in accepted)
        usage = raw.get("_usage") if isinstance(raw, dict) else None
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool):
                prompt_tokens = None
            if not isinstance(completion_tokens, int) or isinstance(completion_tokens, bool):
                completion_tokens = None
            record_tokens = getattr(store, "job_record_model_tokens", None)
            if callable(record_tokens) and not await record_tokens(
                job_id, fence, prompt_tokens, completion_tokens
            ):
                return 0
        async with store.pool.acquire() as conn:
            await publish_model(conn, embed, claims, job_id, fence)
        # publish_model embeds but does not settle; a stale fence makes this False.
        return 1 if await store.job_done(job_id, fence) else 0
    except Exception as exc:
        await _settle_failure(store, job_id, fence, "extract_model", exc)
        return 0


_BOUNDED_EXCERPT_CHARS = 300


def _bounded_excerpt(text: str) -> str:
    """A length-capped excerpt for ask_summary/outcome_summary in this
    rules-only slice. NOT a real summarizer (that is T5.6's job) --
    bounded so a work_item never carries an unbounded pasted wall of
    text, but still just an excerpt, not a model-condensed summary."""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= _BOUNDED_EXCERPT_CHARS:
        return text
    return text[: _BOUNDED_EXCERPT_CHARS - 15].rstrip() + " ...(truncated)"


async def _run_work(store, worker: str) -> int:
    """Spec 5 T5.3 (scoped slice): claim and process one `extract_work`
    job. Strips memory blocks before anything else touches the text,
    keeps every work_item at status='unknown' (self-reports and work_event
    structural signals are supporting evidence only -- the confirmation
    path from an explicit user verdict is T5.4's job, not built here),
    and is idempotent on the job's own op_id via
    ON CONFLICT (owner, source_op_id) DO NOTHING. Never raises."""
    from .turn_filter import segment_turn, strip_memory_blocks

    claimed = await store.job_claim("extract_work", worker, 1, lease_s=WORK_LEASE_S,
                                    session_ordered=True)
    if not claimed:
        return 0
    job_id, fence, payload = claimed[0]
    try:
        async with store.pool.acquire() as conn:
            job_row = await conn.fetchrow(
                "SELECT owner, op_id FROM durable_job WHERE id = $1", int(job_id),
            )
            if job_row is None:
                return 1 if await store.job_done(job_id, fence) else 0
            owner = str(job_row["owner"])
            op_id = str(job_row["op_id"])
            session_id = str(payload.get("session_id") or "")
            first_id = payload.get("first_span_id")
            last_id = payload.get("last_span_id")
            if not session_id or first_id is None or last_id is None:
                return 1 if await store.job_done(job_id, fence) else 0

            spans = await conn.fetch(
                "SELECT id, role, content FROM conversations "
                "WHERE agent_identity = $1 AND session_id = $2 AND id BETWEEN $3 AND $4 "
                "ORDER BY id",
                owner, session_id, int(first_id), int(last_id),
            )
            if not spans:
                return 1 if await store.job_done(job_id, fence) else 0

            # T4.3: strip injected memory blocks before anything else
            # touches the text, so a reply quoting its own injected
            # memory can never feed back into a work item.
            authored_by_span: dict[int, str] = {}
            for span in spans:
                stripped = strip_memory_blocks(str(span["content"] or ""))
                authored = "".join(
                    stripped[seg.start:seg.end]
                    for seg in segment_turn(stripped) if seg.kind == "authored"
                )
                if authored.strip():
                    authored_by_span[int(span["id"])] = authored

            if not authored_by_span:
                return 1 if await store.job_done(job_id, fence) else 0

            user_spans = [s for s in spans if s["role"] == "user" and int(s["id"]) in authored_by_span]
            assistant_spans = [
                s for s in spans if s["role"] == "assistant" and int(s["id"]) in authored_by_span
            ]
            ask_span = user_spans[0] if user_spans else spans[0]
            ask_span_id = int(ask_span["id"])
            ask_summary = _bounded_excerpt(authored_by_span.get(ask_span_id, ""))

            outcome_span = assistant_spans[-1] if assistant_spans else None
            outcome_span_id = int(outcome_span["id"]) if outcome_span is not None else None
            outcome_summary = (
                _bounded_excerpt(authored_by_span[outcome_span_id])
                if outcome_span_id is not None else None
            )

            # Self-reports (assistant-only accounts, even ones using success
            # language like "tests pass") and work_event status codes are
            # supporting evidence only -- they already sit in work_event,
            # joinable by (owner, session_id, span range), for a future
            # confirmation step to consult. Nothing in this slice promotes
            # status past 'unknown': only an explicit user verdict can, and
            # that confirmation path is T5.4's job, not this one's.
            status = "unknown"

            topic_key = f"session:{session_id}"
            async with conn.transaction():
                item_row = await conn.fetchrow(
                    "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, "
                    "ask_summary, outcome_summary, status, ask_span_id, outcome_span_id, "
                    "extractor_version) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                    "ON CONFLICT (owner, source_op_id) DO NOTHING "
                    "RETURNING id",
                    owner, session_id, topic_key, op_id, ask_summary, outcome_summary,
                    status, ask_span_id, outcome_span_id, WORK_VERSION,
                )
                if item_row is not None:
                    await conn.execute(
                        "INSERT INTO work_item_current (owner, topic_key, work_item_id) "
                        "VALUES ($1, $2, $3) "
                        "ON CONFLICT (owner, topic_key) DO UPDATE SET work_item_id = EXCLUDED.work_item_id",
                        owner, topic_key, int(item_row["id"]),
                    )
        return 1 if await store.job_done(job_id, fence) else 0
    except Exception as exc:
        await _settle_failure(store, job_id, fence, "extract_work", exc)
        return 0


async def run_extraction_once(provider, store, *, embed: EmbedFn, model: Optional[ModelFn]) -> int:
    """Claim and settle at most one rules job and one model job. Returns jobs finished.

    With `model=None` the model tier is skipped and its jobs stay queued.
    Never raises.
    """
    worker = str(getattr(provider, "_outbox_owner", "") or "extraction")
    allow_assistant = bool(getattr(getattr(provider, "config", None),
                                   "extract_assistant_replies", False))
    work_capture = bool(getattr(getattr(provider, "config", None), "work_capture", False))
    finished = 0
    for step in (lambda: _run_rules(store, worker, embed),
                 (lambda: _run_model(store, worker, model, embed,
                                     allow_assistant=allow_assistant)) if model is not None else None,
                 (lambda: _run_work(store, worker)) if work_capture else None):
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
