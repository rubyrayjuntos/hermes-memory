"""Spec 4 T4.1 follow-up: an assistant reply whose preceding user turn was
dropped as noise has no `previous_conversation_id` to pair with. It must
start its own standalone batch, never silently append to some OTHER
pending batch for the same session (the append-by-arrival-order risk
flagged in review)."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_unpaired_reply_never_attaches_to_an_unrelated_pending_batch(
    store, clean_hermes_test_db,
):
    """An unrelated user span (999) is already accumulating a pending batch
    in the same session when the orphan reply (555) arrives with no
    previous_conversation_id. The orphan must NOT end up in span 999's job."""
    unrelated_span = 999
    orphan_reply_span = 555
    await store.job_add_model_batch("alice", "s3", unrelated_span)

    await store.job_add_model_batch_standalone("alice", "s3", orphan_reply_span)

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM durable_job WHERE owner = 'alice' AND kind = 'extract_model' "
            "AND session_id = 's3' ORDER BY created_at"
        )
    import json
    payloads = [dict(json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"])
                for r in rows]

    unrelated_job = next((p for p in payloads if unrelated_span in p.get("span_ids", [])), None)
    assert unrelated_job is not None
    assert orphan_reply_span not in unrelated_job["span_ids"], (
        "orphan reply silently attached to an unrelated pending batch"
    )

    orphan_job = next((p for p in payloads if orphan_reply_span in p.get("span_ids", [])), None)
    assert orphan_job is not None, "orphan reply was dropped instead of starting its own batch"
    assert orphan_job["span_ids"] == [orphan_reply_span]
