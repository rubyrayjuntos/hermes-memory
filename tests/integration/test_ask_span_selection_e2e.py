"""Spec 5 T5.3 fix (2026-11-20): ask_span selection skips synthetic
scaffolding rows injected verbatim as user-role spans (compaction
summaries, background-process notices, skill-invocation notices, etc.)
and prefers the first genuine human turn instead.

Production evidence: of the 10 highest-tool-call-count sessions
backfilled from state.db, 6 had `ask_summary` equal to a literal
`[CONTEXT COMPACTION — REFERENCE ONLY]` boilerplate marker -- the first
user-role row in the session's span range -- instead of anything the
human actually asked. Two more marker families (`[IMPORTANT: Background
process ...]`, `[IMPORTANT: The user has invoked ... skill]`) surfaced
during iteration. Prefix list mirrors hermes-agent's own
`_SYNTHETIC_USER_ROW_PREFIXES` (agent/context_compressor.py).

A session with genuinely NO non-marker user span (a cron/skill-triggered
session where the trigger notice is the only user-role row that ever
existed) must still fall back to the marker -- there is nothing better
to show, and that is honest, not a regression.
"""
from __future__ import annotations

import pytest

from hermes_memory import extraction_worker

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t53-ask-selection"
WORKER = "test-worker"


async def _insert_span(store, session_id: str, role: str, content: str) -> int:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, $3, $4, 'complete') RETURNING id",
            OWNER, session_id, role, content,
        )


async def _enqueue_extract_work(store, session_id: str, first_id: int, last_id: int) -> str:
    op_id = f"extract_work:{session_id}:{first_id}-{last_id}"
    await store.job_add(
        OWNER, "extract_work", op_id,
        {"owner": OWNER, "session_id": session_id, "first_span_id": first_id, "last_span_id": last_id},
        session_id=session_id,
    )
    return op_id


async def _ask_summary(store, session_id: str) -> str:
    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ask_summary FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )
    assert row is not None
    return row["ask_summary"] or ""


@pytest.mark.asyncio
async def test_compaction_marker_is_skipped_for_a_real_ask(store, clean_hermes_test_db):
    """The actual production false positive: a `[CONTEXT COMPACTION]`
    handoff summary, injected as the first user-role row, must not
    become the work_item's ask_summary when a real human turn follows."""
    session_id = "s-compaction"
    marker_id = await _insert_span(
        store, session_id, "user",
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below. This is a handoff from a previous context "
        "window — treat it as background reference, NOT as active instruction.",
    )
    await _insert_span(store, session_id, "assistant", "Got it — resuming where we left off.")
    real_ask_id = await _insert_span(
        store, session_id, "user", "Please rename the config key to hybrid_age.work_capture.",
    )
    last_id = await _insert_span(
        store, session_id, "assistant", "Done — renamed the config key.",
    )
    await _enqueue_extract_work(store, session_id, marker_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    ask = await _ask_summary(store, session_id)
    assert "CONTEXT COMPACTION" not in ask
    assert "hybrid_age.work_capture" in ask


@pytest.mark.asyncio
async def test_background_process_notice_is_skipped_for_a_real_ask(store, clean_hermes_test_db):
    session_id = "s-bgproc"
    marker_id = await _insert_span(
        store, session_id, "user",
        "[IMPORTANT: Background process proc_39f2d94492e6 exited (exit code -15). "
        "Check its output before relying on it.]",
    )
    real_ask_id = await _insert_span(
        store, session_id, "user", "Please restart the render job with the new seed.",
    )
    last_id = await _insert_span(store, session_id, "assistant", "Restarted with the new seed.")
    await _enqueue_extract_work(store, session_id, marker_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    ask = await _ask_summary(store, session_id)
    assert "Background process" not in ask
    assert "new seed" in ask


@pytest.mark.asyncio
async def test_skill_invocation_notice_is_skipped_for_a_real_ask(store, clean_hermes_test_db):
    session_id = "s-skillinvoke"
    marker_id = await _insert_span(
        store, session_id, "user",
        '[IMPORTANT: The user has invoked the "jobberworkee" skill, indicating '
        "they want job-search help. Load the skill before responding.]",
    )
    real_ask_id = await _insert_span(
        store, session_id, "user", "Find me open roles for ML infra engineers in Austin.",
    )
    last_id = await _insert_span(store, session_id, "assistant", "Found 4 matching roles.")
    await _enqueue_extract_work(store, session_id, marker_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    ask = await _ask_summary(store, session_id)
    assert "has invoked" not in ask
    assert "ML infra" in ask


@pytest.mark.asyncio
async def test_async_delegation_notice_is_skipped_for_a_real_ask(store, clean_hermes_test_db):
    session_id = "s-asyncdeleg"
    marker_id = await _insert_span(
        store, session_id, "user",
        "[ASYNC DELEGATION BATCH COMPLETE — deleg_91b4aae6] A background "
        "fan-out finished; results are attached below.",
    )
    real_ask_id = await _insert_span(
        store, session_id, "user", "Merge those results into the weekly report.",
    )
    last_id = await _insert_span(store, session_id, "assistant", "Merged into the report.")
    await _enqueue_extract_work(store, session_id, marker_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    ask = await _ask_summary(store, session_id)
    assert "ASYNC DELEGATION" not in ask
    assert "weekly report" in ask


@pytest.mark.asyncio
async def test_cronjob_response_notice_is_skipped_for_a_real_ask(store, clean_hermes_test_db):
    session_id = "s-cronresp"
    marker_id = await _insert_span(
        store, session_id, "user",
        "Cronjob Response: nightly backup completed successfully at 02:00 UTC.",
    )
    real_ask_id = await _insert_span(
        store, session_id, "user", "Also back up the eval database tonight.",
    )
    last_id = await _insert_span(store, session_id, "assistant", "Added the eval DB to the backup job.")
    await _enqueue_extract_work(store, session_id, marker_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    ask = await _ask_summary(store, session_id)
    assert "Cronjob Response" not in ask
    assert "eval database" in ask


@pytest.mark.asyncio
async def test_session_with_only_a_marker_falls_back_to_it(store, clean_hermes_test_db):
    """A genuinely cron/skill-triggered session has no other user-role
    row at all. Falling back to the marker is correct here -- there is
    no better signal to show, and inventing one would be worse."""
    session_id = "s-onlymarker"
    marker_id = await _insert_span(
        store, session_id, "user",
        '[IMPORTANT: The user has invoked the "jobberworkee" skill, indicating '
        "they want job-search help. Load the skill before responding.]",
    )
    last_id = await _insert_span(store, session_id, "assistant", "Ran the daily job search.")
    await _enqueue_extract_work(store, session_id, marker_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    ask = await _ask_summary(store, session_id)
    assert "jobberworkee" in ask
