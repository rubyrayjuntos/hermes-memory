"""Spec 4 T4.1: a user turn and its assistant reply must land in the same
`durable_job` model batch even when the batch would otherwise fill or flush
between the two enqueue calls (append-by-arrival-order cannot guarantee
this -- see the Spec 4 review gap)."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_pair_stays_together_when_batch_already_full(store, clean_hermes_test_db):
    """Fill a batch to MODEL_BATCH_SIZE (8) with unrelated spans first, then
    enqueue a user span + its paired reply span. Both must end up in the
    SAME durable_job row, not split across the full batch and a new one."""
    for span_id in range(1, 9):
        await store.job_add_model_batch("alice", "s", span_id)

    user_span, reply_span = 100, 101
    await store.job_add_model_batch("alice", "s", user_span)
    await store.job_add_model_batch_paired(
        "alice", "s", reply_span, paired_with=user_span,
    )

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM durable_job WHERE owner = 'alice' AND kind = 'extract_model' "
            "ORDER BY created_at"
        )
    import json
    payloads = [dict(json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"])
                for r in rows]
    pair_job = next((p for p in payloads if user_span in p.get("span_ids", [])), None)
    assert pair_job is not None, "user span not found in any batch"
    assert reply_span in pair_job["span_ids"], (
        "reply span landed in a different batch than its paired user span"
    )


@pytest.mark.asyncio
async def test_pair_stays_together_when_original_batch_already_flushed(store, clean_hermes_test_db):
    """If the user span's batch already left 'pending' (claimed/flushed)
    before the reply arrives, pairing still lands the reply with the user
    span -- in a fresh batch, not scattered into an unrelated one."""
    user_span, reply_span = 200, 201
    await store.job_add_model_batch("alice", "s2", user_span)

    # Force the user-only batch past MODEL_BATCH_MAX_WAIT_S so it is
    # claimable below threshold (mirrors test_model_batch_claims_after_max_wait),
    # then claim it -- simulating the flush landing before the reply arrives.
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE durable_job SET payload = jsonb_set(payload, '{batch_started_at}', "
            "to_jsonb((now() - interval '31 minutes')::text), true) "
            "WHERE owner = 'alice' AND session_id = 's2' AND kind = 'extract_model'"
        )
    claimed = await store.job_claim(
        "extract_model", "w", 1, lease_s=120, session_ordered=True,
        ready_model_batch=True,
    )
    assert claimed, "setup: expected the user-only batch to be claimable after max-wait flush"

    await store.job_add_model_batch_paired(
        "alice", "s2", reply_span, paired_with=user_span,
    )

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, payload FROM durable_job WHERE owner = 'alice' "
            "AND kind = 'extract_model' ORDER BY created_at"
        )
    import json
    found_pairing_job = False
    for r in rows:
        payload = dict(json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"])
        span_ids = payload.get("span_ids", [])
        if reply_span in span_ids:
            assert user_span in span_ids, (
                "reply span got its own batch without the paired user span"
            )
            found_pairing_job = True
    assert found_pairing_job, "reply span was never enqueued into any batch"
