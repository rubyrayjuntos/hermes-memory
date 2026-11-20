"""Spec 5 T5.3: extraction_worker._run_work, the extract_work job processor.

Covers the T5.3 requirements plus T5.4 (status-source priority) and
T5.6 (rule-based summaries), now that all are built:
  1. strip_memory_blocks runs before extraction touches the conversation text.
  2. Self-reports (assistant-only prose) never set status on their own --
     only an explicit user verdict span (T5.4) or a work_event
     status_code (T5.4, tier 2) can, in that priority order.
  3. A retried extract_work job writes each work_item exactly once
     (ON CONFLICT (owner, source_op_id) DO NOTHING, keyed on the job's
     own op_id).
  4. work_capture off means the worker never claims an extract_work job,
     even when one already exists in the table.
  5. (T5.6) ask_summary/outcome_summary are rule-based extractive
     summaries of AUTHORED conversation text only -- a pasted code
     block never reaches them, and neither does work_event's raw tool
     output (which _run_work never reads for summary content at all,
     only for its structural status_code -- see test_run_work_e2e.py's
     T5.4 tests).

Artifact verification (T5.5) is explicitly out of scope here.
"""
from __future__ import annotations

import pytest

from hermes_memory import extraction_worker
from hermes_memory.config import HybridAgeConfig

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t53-e2e"
WORKER = "test-worker"


class _FakeEmbedder:
    async def embed_text(self, text: str, *, as_query: bool = False):
        vec = [0.0] * 768
        vec[0] = 0.5
        return vec


async def _embed(texts):
    return [[0.5] + [0.0] * 767 for _ in texts]


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


def _fake_provider(*, work_capture: bool):
    from types import SimpleNamespace
    return SimpleNamespace(
        _outbox_owner="w", config=HybridAgeConfig(dsn="unused", work_capture=work_capture),
    )


@pytest.mark.asyncio
async def test_memory_block_is_stripped_before_extraction(store, clean_hermes_test_db):
    session_id = "s1"
    quoting_reply = (
        "Here's what I remember:\n<memory>\n  you lives_in Reykjavik\n</memory>\nAll set."
    )
    first_id = await _insert_span(store, session_id, "user", "What do you remember about me?")
    last_id = await _insert_span(store, session_id, "assistant", quoting_reply)
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome_summary FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )
    assert row is not None
    assert "Reykjavik" not in (row["outcome_summary"] or "")


@pytest.mark.asyncio
async def test_pasted_code_block_never_reaches_the_summary_t56(store, clean_hermes_test_db):
    """T5.6: a pasted code block (segment_turn's 'pasted' kind) is
    excluded from summarization input entirely -- it never appears in
    ask_summary/outcome_summary, not even truncated or excerpted."""
    session_id = "s1b"
    secret_marker = "SECRET_API_KEY_zzz999"
    pasted_reply = (
        "Done, see the diff below.\n"
        "```python\n"
        f"API_KEY = '{secret_marker}'\n"
        "def run():\n    pass\n"
        "```\n"
        "All set."
    )
    first_id = await _insert_span(store, session_id, "user", "Paste the fix please.")
    last_id = await _insert_span(store, session_id, "assistant", pasted_reply)
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome_summary FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )
    assert row is not None
    assert secret_marker not in (row["outcome_summary"] or "")
    assert "```" not in (row["outcome_summary"] or "")


@pytest.mark.asyncio
async def test_assistant_only_self_report_stays_unconfirmed(store, clean_hermes_test_db):
    session_id = "s2"
    first_id = await _insert_span(store, session_id, "user", "Please fix the bug in provider.py.")
    last_id = await _insert_span(
        store, session_id, "assistant",
        "I fixed the bug in provider.py and the tests pass now.",
    )
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert row["status"] == "unknown"


@pytest.mark.asyncio
async def test_explicit_user_verdict_now_confirms_status_t54(store, clean_hermes_test_db):
    """T5.4 (status-source priority) is now built: an explicit,
    unambiguous user verdict span DOES set status, with reason_span_id
    pointing at the span that said it -- this supersedes the old
    "stays unconfirmed in this slice" placeholder from before T5.4 was
    assigned and built."""
    session_id = "s3"
    first_id = await _insert_span(store, session_id, "user", "Please run the eval.")
    await _insert_span(store, session_id, "assistant", "Done, the eval passed.")
    last_id = await _insert_span(
        store, session_id, "user", "These numbers are fake, that eval did not really pass.",
    )
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, reason, reason_span_id FROM work_item "
            "WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert row["status"] == "failed"
    assert row["reason"]
    assert row["reason_span_id"] == last_id


@pytest.mark.asyncio
async def test_work_event_failure_promotes_when_no_user_verdict_exists_t54(
    store, clean_hermes_test_db,
):
    """T5.4: with no explicit user verdict present, a test/eval tool's
    non-zero status_code in work_event is tier-2 evidence and DOES set
    status to failed -- self-reports (the assistant's own prose) never
    do this on their own, but a real tool exit code is a different,
    structural signal the spec explicitly ranks above 'unknown'."""
    session_id = "s4"
    first_id = await _insert_span(store, session_id, "user", "Run the test suite please.")
    last_id = await _insert_span(store, session_id, "assistant", "I ran the suite, done.")
    async with store.pool.acquire() as conn:
        event_id = await conn.fetchval(
            "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, status_code, summary) "
            "VALUES ($1, $2, $3, 'uid-1:0', 'run_tests', 1, 'run_tests: 42 chars') RETURNING id",
            OWNER, session_id, last_id,
        )
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, reason, reason_event_id FROM work_item "
            "WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert row["status"] == "failed"
    assert row["reason"]
    assert row["reason_event_id"] == event_id


@pytest.mark.asyncio
async def test_work_event_success_alone_stays_unknown_t54(store, clean_hermes_test_db):
    """Tightened T5.4 rule: a bare success status_code with no user
    verdict must NOT confirm 'done' on its own -- a command can exit 0
    while the actual work failed. This is the exact false confirmation
    Spec 4 was built to prevent, one layer down."""
    session_id = "s5"
    first_id = await _insert_span(store, session_id, "user", "Run the test suite please.")
    last_id = await _insert_span(store, session_id, "assistant", "I ran the suite, done.")
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, status_code, summary) "
            "VALUES ($1, $2, $3, 'uid-1:0', 'run_tests', 0, 'run_tests: 42 chars')",
            OWNER, session_id, last_id,
        )
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, reason_event_id FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )
    assert row["status"] == "unknown"
    assert row["reason_event_id"] is None


@pytest.mark.asyncio
async def test_later_user_turn_overrides_an_earlier_verdict_t54(store, clean_hermes_test_db):
    """A later user turn can always reverse an earlier abandon/confirm --
    the spec's own condition for letting 'abandoned' be text-inferred at
    all. Picks the LAST matching span's id as reason_span_id, not the
    first."""
    session_id = "s6"
    first_id = await _insert_span(store, session_id, "user", "Please build the thing.")
    await _insert_span(store, session_id, "user", "Never mind, forget it.")
    last_id = await _insert_span(store, session_id, "user", "Actually, confirmed, go ahead.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, reason_span_id FROM work_item WHERE owner = $1 AND session_id = $2",
            OWNER, session_id,
        )
    assert row["status"] == "done"
    assert row["reason_span_id"] == last_id


@pytest.mark.asyncio
async def test_retrying_the_same_job_writes_the_work_item_once(store, clean_hermes_test_db):
    session_id = "s5"
    first_id = await _insert_span(store, session_id, "user", "Please summarize the sprint plan.")
    last_id = await _insert_span(
        store, session_id, "assistant", "I put together a short summary of the sprint plan.",
    )
    op_id = await _enqueue_extract_work(store, session_id, first_id, last_id)

    finished = await extraction_worker._run_work(store, WORKER)
    assert finished == 1

    # Simulate a retry: re-insert the SAME op_id as a fresh pending job
    # (as if the lease expired and the job was reclaimed) and process it again.
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE durable_job SET status = 'pending', attempts = 0 "
            "WHERE owner = $1 AND kind = 'extract_work' AND op_id = $2",
            OWNER, op_id,
        )
    finished_again = await extraction_worker._run_work(store, WORKER)
    assert finished_again == 1

    async with store.pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert count == 1, "a retried job must never create a second work_item row"


@pytest.mark.asyncio
async def test_work_capture_off_never_claims_an_extract_work_job(store, clean_hermes_test_db):
    session_id = "s6"
    first_id = await _insert_span(store, session_id, "user", "Please do a thing.")
    last_id = await _insert_span(store, session_id, "assistant", "I did the thing.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    provider = _fake_provider(work_capture=False)
    finished = await extraction_worker.run_extraction_once(provider, store, embed=_embed, model=None)
    assert finished == 0

    async with store.pool.acquire() as conn:
        job_status = await conn.fetchval(
            "SELECT status FROM durable_job WHERE owner = $1 AND kind = 'extract_work'", OWNER,
        )
        item_count = await conn.fetchval(
            "SELECT count(*) FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert job_status == "pending", "the job row must be untouched -- never even claimed"
    assert item_count == 0


@pytest.mark.asyncio
async def test_work_capture_on_claims_and_processes_the_job(store, clean_hermes_test_db):
    session_id = "s7"
    first_id = await _insert_span(store, session_id, "user", "Please do another thing.")
    last_id = await _insert_span(store, session_id, "assistant", "I did the other thing.")
    await _enqueue_extract_work(store, session_id, first_id, last_id)

    provider = _fake_provider(work_capture=True)
    finished = await extraction_worker.run_extraction_once(provider, store, embed=_embed, model=None)
    assert finished == 1

    async with store.pool.acquire() as conn:
        item_count = await conn.fetchval(
            "SELECT count(*) FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    assert item_count == 1
