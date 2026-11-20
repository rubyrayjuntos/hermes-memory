"""Spec 5 T5.5: artifact verification wired into the extraction worker.

Verification runs only inside _run_work (per the spec: "never at
prefetch"), only against repos in config.work_verify_repo_roots, and
never marks an artifact passed from anything but a real local,
network-free check (see work_artifact_verify.py / test_work_artifact_verify.py
for the check logic itself -- this file proves the store-level wiring:
updates land in work_artifact.exists_now/content_hash/verified_at, and
an unverifiable artifact is kept and flagged, never dropped).
"""
from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec5-t55-verify"


async def _insert_work_item(store, session_id: str) -> int:
    async with store.pool.acquire() as conn:
        span_id = await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, 'user', 'test span', 'complete') RETURNING id",
            OWNER, session_id,
        )
        return await conn.fetchval(
            "INSERT INTO work_item (owner, session_id, topic_key, source_op_id, ask_summary, "
            "status, ask_span_id, extractor_version) "
            "VALUES ($1, $2, $3, $4, $5, 'unknown', $6, '1') RETURNING id",
            OWNER, session_id, f"session:{session_id}", f"op:{session_id}", "test ask", span_id,
        )


async def _insert_artifact(store, work_item_id: int, kind: str, ref: str, repo: str) -> int:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO work_artifact (owner, work_item_id, kind, ref, repo) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            OWNER, work_item_id, kind, ref, repo,
        )


@pytest.mark.asyncio
async def test_existing_allowlisted_file_is_marked_verified(store, clean_hermes_test_db, tmp_path):
    repo_root = str(tmp_path)
    with open(os.path.join(repo_root, "thing.py"), "w") as f:
        f.write("print(1)\n")

    item_id = await _insert_work_item(store, "s1")
    artifact_id = await _insert_artifact(store, item_id, "file", "thing.py", repo_root)

    verified, skipped = await store.verify_work_artifacts(OWNER, item_id, [repo_root])
    assert verified == 1
    assert skipped == 0

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT exists_now, content_hash, verified_at FROM work_artifact WHERE id = $1", artifact_id,
        )
    assert row["exists_now"] is True
    assert row["content_hash"] is not None
    assert row["verified_at"] is not None


@pytest.mark.asyncio
async def test_unverifiable_artifact_is_kept_and_flagged_not_dropped(
    store, clean_hermes_test_db, tmp_path,
):
    """A PR-kind artifact has no available check -- it must stay in the
    table with exists_now/content_hash/verified_at untouched (NULL),
    never deleted and never written as passed."""
    repo_root = str(tmp_path)
    item_id = await _insert_work_item(store, "s2")
    artifact_id = await _insert_artifact(store, item_id, "pr", "108", repo_root)

    verified, skipped = await store.verify_work_artifacts(OWNER, item_id, [repo_root])
    assert verified == 0
    assert skipped == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT exists_now, content_hash, verified_at FROM work_artifact WHERE id = $1", artifact_id,
        )
    assert row is not None, "the unverifiable artifact must still exist, never dropped"
    assert row["exists_now"] is None
    assert row["content_hash"] is None
    assert row["verified_at"] is None


@pytest.mark.asyncio
async def test_repo_outside_the_allowlist_is_never_checked(store, clean_hermes_test_db, tmp_path):
    repo_root = str(tmp_path)
    with open(os.path.join(repo_root, "thing.py"), "w") as f:
        f.write("x")
    item_id = await _insert_work_item(store, "s3")
    artifact_id = await _insert_artifact(store, item_id, "file", "thing.py", repo_root)

    verified, skipped = await store.verify_work_artifacts(
        OWNER, item_id, ["/some/other/not-allowlisted/repo"],
    )
    assert verified == 0
    assert skipped == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT exists_now, verified_at FROM work_artifact WHERE id = $1", artifact_id,
        )
    assert row["exists_now"] is None
    assert row["verified_at"] is None


@pytest.mark.asyncio
async def test_run_work_verifies_artifacts_for_the_item_it_just_processed(
    store, clean_hermes_test_db, tmp_path,
):
    """End-to-end through _run_work itself (not just the store method):
    verification runs only inside the extraction worker, never at
    prefetch, and only against configured repo_roots. Exercised via the
    retry path (a work_artifact row inserted for an item _run_work has
    already written, then the same job reprocessed) since this slice
    has no artifact-origination pathway yet -- a work item can exist
    with zero artifacts until some future task populates them from
    work_event.refs."""
    from hermes_memory import extraction_worker

    session_id = "s4"
    repo_root = str(tmp_path)
    with open(os.path.join(repo_root, "built.py"), "w") as f:
        f.write("print('built')\n")

    async with store.pool.acquire() as conn:
        first_id = await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, 'user', 'Please build the thing.', 'complete') RETURNING id",
            OWNER, session_id,
        )
        last_id = await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, 'assistant', 'Built it.', 'complete') RETURNING id",
            OWNER, session_id,
        )
    op_id = f"extract_work:{session_id}:{first_id}-{last_id}"
    await store.job_add(
        OWNER, "extract_work", op_id,
        {"owner": OWNER, "session_id": session_id, "first_span_id": first_id, "last_span_id": last_id},
        session_id=session_id,
    )
    assert await extraction_worker._run_work(store, "test-worker", [repo_root]) == 1

    async with store.pool.acquire() as conn:
        item_id = await conn.fetchval(
            "SELECT id FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    artifact_id = await _insert_artifact(store, item_id, "file", "built.py", repo_root)

    # Simulate a retry (lease expired, job reclaimed): reset the SAME
    # durable_job row to pending rather than re-adding it, matching
    # test_run_work_e2e.py's retry pattern -- re-adding the same op_id
    # would hit ON CONFLICT DO NOTHING against the already-done row.
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE durable_job SET status = 'pending', attempts = 0 "
            "WHERE owner = $1 AND kind = 'extract_work' AND op_id = $2",
            OWNER, op_id,
        )
    assert await extraction_worker._run_work(store, "test-worker", [repo_root]) == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT exists_now, content_hash FROM work_artifact WHERE id = $1", artifact_id,
        )
    assert row["exists_now"] is True
    assert row["content_hash"] is not None


@pytest.mark.asyncio
async def test_run_work_never_verifies_when_no_repo_roots_configured(
    store, clean_hermes_test_db, tmp_path,
):
    """repo_roots defaults to an empty list -- _run_work must not even
    query work_artifact when nothing is allowlisted."""
    from hermes_memory import extraction_worker

    session_id = "s5"
    repo_root = str(tmp_path)
    with open(os.path.join(repo_root, "built.py"), "w") as f:
        f.write("print('built')\n")

    async with store.pool.acquire() as conn:
        first_id = await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, 'user', 'Please build the thing.', 'complete') RETURNING id",
            OWNER, session_id,
        )
        last_id = await conn.fetchval(
            "INSERT INTO conversations (agent_identity, session_id, role, content, drain_status) "
            "VALUES ($1, $2, 'assistant', 'Built it.', 'complete') RETURNING id",
            OWNER, session_id,
        )
    op_id = f"extract_work:{session_id}:{first_id}-{last_id}"
    await store.job_add(
        OWNER, "extract_work", op_id,
        {"owner": OWNER, "session_id": session_id, "first_span_id": first_id, "last_span_id": last_id},
        session_id=session_id,
    )
    assert await extraction_worker._run_work(store, "test-worker", []) == 1

    async with store.pool.acquire() as conn:
        item_id = await conn.fetchval(
            "SELECT id FROM work_item WHERE owner = $1 AND session_id = $2", OWNER, session_id,
        )
    artifact_id = await _insert_artifact(store, item_id, "file", "built.py", repo_root)

    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE durable_job SET status = 'pending', attempts = 0 "
            "WHERE owner = $1 AND kind = 'extract_work' AND op_id = $2",
            OWNER, op_id,
        )
    assert await extraction_worker._run_work(store, "test-worker", []) == 1

    async with store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT exists_now, verified_at FROM work_artifact WHERE id = $1", artifact_id,
        )
    assert row["exists_now"] is None
    assert row["verified_at"] is None

