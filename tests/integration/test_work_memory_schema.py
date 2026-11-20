"""Spec 5 T5.4: schema-level invariants for work_item/work_artifact/work_event.
Pure-schema tests only here -- app logic (T5.1-T5.3, T5.5-T5.7) gets its own
test files once it exists. Never touches claims/claim_evidence."""
from __future__ import annotations

import asyncpg
import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-schema"


async def _insert_span(conn, session_id: str, role: str = "user",
                       content: str = "placeholder span content for FK targets") -> int:
    return await conn.fetchval(
        "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
        "VALUES ($1, $2, $3, $4, 'complete') RETURNING id",
        OWNER, session_id, role, content,
    )


@pytest.mark.asyncio
async def test_work_capture_never_writes_to_claims_tables(store, clean_hermes_test_db):
    async with store.pool.acquire() as conn:
        span_id = await _insert_span(conn, "s1")
        await conn.execute(
            "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, "
            "ask_summary, status, ask_span_id, extractor_version) "
            "VALUES ($1, 's1', 'shadow-replay', 'op-1', 'build shadow replay', "
            "'done', $2, '1')",
            OWNER, span_id,
        )
        claim_count = await conn.fetchval("SELECT count(*) FROM claims WHERE span_id = $1", span_id)
        evidence_count = await conn.fetchval(
            "SELECT count(*) FROM claim_evidence ce JOIN claims c ON c.claim_id = ce.claim_id "
            "WHERE c.span_id = $1", span_id,
        )
    assert claim_count == 0
    assert evidence_count == 0


@pytest.mark.asyncio
async def test_failed_work_item_requires_a_reason_and_a_source(store, clean_hermes_test_db):
    async with store.pool.acquire() as conn:
        span_id = await _insert_span(conn, "s2")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, "
                "ask_summary, status, ask_span_id, extractor_version) "
                "VALUES ($1, 's2', 'shadow-replay', 'op-2', 'build shadow replay', "
                "'failed', $2, '1')",  # status='failed', no reason, no reason source -- must reject
                OWNER, span_id,
            )


@pytest.mark.asyncio
async def test_failed_work_item_with_reason_and_span_is_accepted(store, clean_hermes_test_db):
    async with store.pool.acquire() as conn:
        ask_span = await _insert_span(conn, "s3")
        reason_span = await _insert_span(conn, "s3", role="user",
                                          content="these numbers are fake / placeholder")
        item_id = await conn.fetchval(
            "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, "
            "ask_summary, status, reason, ask_span_id, reason_span_id, extractor_version) "
            "VALUES ($1, 's3', 'shadow-replay', 'op-3', 'build shadow replay', "
            "'failed', 'user verdict: fake placeholder metrics', $2, $3, '1') RETURNING id",
            OWNER, ask_span, reason_span,
        )
    assert item_id is not None


@pytest.mark.asyncio
async def test_work_event_replay_is_deduplicated_by_source_event_id(store, clean_hermes_test_db):
    async with store.pool.acquire() as conn:
        span_id = await _insert_span(conn, "s4")
        await conn.execute(
            "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, status_code) "
            "VALUES ($1, 's4', $2, 'uid-abc', 'test_runner', 0)",
            OWNER, span_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO work_event (owner, session_id, span_id, source_event_id, tool, status_code) "
                "VALUES ($1, 's4', $2, 'uid-abc', 'test_runner', 0)",  # same source_event_id, same session -> reject
                OWNER, span_id,
            )


@pytest.mark.asyncio
async def test_work_artifact_requires_a_known_work_item(store, clean_hermes_test_db):
    async with store.pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO work_artifact (owner, work_item_id, kind, ref) "
                "VALUES ($1, 999999, 'branch', 'shadow-replay-v2')",
                OWNER,
            )


@pytest.mark.asyncio
async def test_work_item_current_supersession_is_atomic_and_queryable(store, clean_hermes_test_db):
    """(a) failed attempt -> (b) later done rebuild supersedes it. (a) stays
    retrievable as a superseded row, not deleted."""
    async with store.pool.acquire() as conn:
        async with conn.transaction():
            ask_a = await _insert_span(conn, "s5")
            reason_a = await _insert_span(conn, "s5", content="these numbers are fake / placeholder")
            item_a = await conn.fetchval(
                "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, ask_summary, "
                "status, reason, ask_span_id, reason_span_id, extractor_version) "
                "VALUES ($1, 's5', 'shadow-replay', 'op-a', 'build shadow replay', 'failed', "
                "'fake metrics', $2, $3, '1') RETURNING id",
                OWNER, ask_a, reason_a,
            )
            await conn.execute(
                "INSERT INTO work_item_current (owner, topic_key, work_item_id) VALUES ($1, 'shadow-replay', $2)",
                OWNER, item_a,
            )

        async with conn.transaction():
            ask_b = await _insert_span(conn, "s5")
            item_b = await conn.fetchval(
                "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, ask_summary, "
                "status, ask_span_id, extractor_version) "
                "VALUES ($1, 's5', 'shadow-replay', 'op-b', 'rebuild shadow replay', 'done', "
                "$2, '1') RETURNING id",
                OWNER, ask_b,
            )
            await conn.execute("UPDATE work_item SET superseded_by = $1 WHERE id = $2", item_b, item_a)
            await conn.execute(
                "UPDATE work_item_current SET work_item_id = $1 WHERE owner = $2 AND topic_key = 'shadow-replay'",
                item_b, OWNER,
            )

        current_id = await conn.fetchval(
            "SELECT work_item_id FROM work_item_current WHERE owner = $1 AND topic_key = 'shadow-replay'",
            OWNER,
        )
        assert current_id == item_b

        superseded_row = await conn.fetchrow(
            "SELECT status, superseded_by FROM work_item WHERE id = $1", item_a,
        )
        assert superseded_row["status"] == "failed"  # (a)'s own status is untouched by supersession
        assert superseded_row["superseded_by"] == item_b  # but it now points at its successor

        still_queryable = await conn.fetchval(
            "SELECT ask_summary FROM work_item WHERE id = $1", item_a,
        )
        assert still_queryable == "build shadow replay"  # (a) still exists, still readable


@pytest.mark.asyncio
async def test_extract_work_is_a_valid_durable_job_kind(store, clean_hermes_test_db):
    async with store.pool.acquire() as conn:
        job_id = await conn.fetchval(
            "INSERT INTO durable_job (owner, kind, op_id, session_id) "
            "VALUES ($1, 'extract_work', 'op-work-1', 's6') RETURNING id",
            OWNER,
        )
    assert job_id is not None


@pytest.mark.asyncio
async def test_superseded_by_cannot_cross_owners(store, clean_hermes_test_db):
    other_owner = "spec5-schema-other"
    async with store.pool.acquire() as conn:
        ask_a = await _insert_span(conn, "s7")
        item_a = await conn.fetchval(
            "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, ask_summary, "
            "status, ask_span_id, extractor_version) "
            "VALUES ($1, 's7', 'topic', 'op-cross-a', 'ask', 'done', $2, '1') RETURNING id",
            OWNER, ask_a,
        )
        other_ask = await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, 's7', 'user', 'placeholder span content for FK targets', 'complete') RETURNING id",
            other_owner,
        )
        other_item = await conn.fetchval(
            "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, ask_summary, "
            "status, ask_span_id, extractor_version) "
            "VALUES ($1, 's7', 'topic', 'op-cross-b', 'ask', 'done', $2, '1') RETURNING id",
            other_owner, other_ask,
        )
        # A composite (owner, superseded_by) FK must reject pointing item_a (owner=OWNER)
        # at other_item, which only exists under other_owner.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE work_item SET superseded_by = $1 WHERE id = $2 AND owner = $3",
                other_item, item_a, OWNER,
            )
