# src/hermes_memory/store_jobs.py
"""Durable jobs: one lease-and-fence lifecycle for capture and extraction (spec §5.1).

The fence increments on every claim; every settle, heartbeat and publish
requires the current fence, so a worker whose lease expired cannot act on a
row someone else now holds. The outbox_* methods are the capture facade with
the Phase 0 signatures.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

JOB_MAX_ATTEMPTS = 5
CAPTURE_LEASE_S = 600.0
_BACKOFF_SQL = "(ARRAY[5, 30, 120, 600])[LEAST(attempts, 4)]"
_LIVE = "('pending', 'leased', 'retryable')"
MODEL_BATCH_SIZE = 8
MODEL_BATCH_IDLE_S = 600
MODEL_BATCH_MAX_WAIT_S = 1800
MODEL_BATCH_MAX_WAIT_S = 1800


def _payload(raw) -> dict:
    return dict(json.loads(raw) if isinstance(raw, str) else raw)


def _count(status: str) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def job_fence_check(conn, job_id: int, fence: int) -> bool:
    """Lock the job row and confirm the caller still holds it. Use inside the publish transaction."""
    row = await conn.fetchval(
        "SELECT 1 FROM durable_job WHERE id = $1 AND fence = $2 AND status = 'leased' FOR UPDATE",
        int(job_id), int(fence))
    return row is not None


class StoreJobsMixin:
    pool: Any

    async def job_add(self, owner: str, kind: str, op_id: str, payload: Dict[str, Any], *,
                      claimed_by: Optional[str] = None, lease_s: float = CAPTURE_LEASE_S,
                      session_id: Optional[str] = None, event_time=None, priority: int = 0):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO durable_job (owner, kind, op_id, payload, session_id, event_time, priority, "
                "status, claimed_by, lease_expires_at, fence) VALUES ($1, $2, $3, $4::jsonb, $5, "
                "coalesce($6, now()), $7, CASE WHEN $8::text IS NULL THEN 'pending' ELSE 'leased' END, $8, "
                "CASE WHEN $8::text IS NULL THEN NULL ELSE now() + make_interval(secs => $9) END, "
                "CASE WHEN $8::text IS NULL THEN 0 ELSE 1 END) "
                "ON CONFLICT (owner, kind, op_id) DO NOTHING RETURNING id, fence",
                owner, kind, op_id, json.dumps(payload), session_id, event_time, int(priority),
                claimed_by, float(lease_s))
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id, fence FROM durable_job WHERE owner = $1 AND kind = $2 AND op_id = $3 "
                    "AND status <> 'done'", owner, kind, op_id)
        return (int(row["id"]), int(row["fence"])) if row is not None else None

    async def job_add_model_batch(self, owner: str, session_id: str, span_id: int, *,
                                  event_time=None) -> None:
        """Append a user span to the latest pending session batch, or create one."""
        span = int(span_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, payload FROM durable_job WHERE owner = $1 AND kind = 'extract_model' "
                    "AND session_id = $2 AND status = 'pending' ORDER BY created_at DESC, id DESC "
                    "LIMIT 1 FOR UPDATE",
                    owner, session_id,
                )
                if row is not None:
                    payload = _payload(row["payload"])
                    span_ids = [int(value) for value in payload.get("span_ids", [])]
                    if span in span_ids:
                        return
                    if len(span_ids) < MODEL_BATCH_SIZE:
                        span_ids.append(span)
                        payload["span_ids"] = span_ids
                        await conn.execute(
                            "UPDATE durable_job SET payload = $2::jsonb, created_at = now() "
                            "WHERE id = $1 AND status = 'pending'",
                            int(row["id"]), json.dumps(payload),
                        )
                        return

                payload = {
                    "span_ids": [span],
                    "extractor": "model",
                    "version": "1",
                    "usage": {"calls": 0},
                }
                op_id = f"{session_id}:window:{span}:1"
                await conn.execute(
                    "INSERT INTO durable_job (owner, kind, op_id, payload, session_id, event_time, "
                    "priority, status, fence) VALUES ($1, 'extract_model', $2, $3::jsonb, $4, "
                    "coalesce($5, now()), 0, 'pending', 0) ON CONFLICT (owner, kind, op_id) DO NOTHING",
                    owner, op_id, json.dumps(payload), session_id, event_time,
                )
                await conn.execute(
                    "UPDATE durable_job SET payload = jsonb_set(payload, '{batch_started_at}', "
                    "to_jsonb(now()::text), true) WHERE owner = $1 AND kind = 'extract_model' "
                    "AND op_id = $2 AND payload->>'batch_started_at' IS NULL",
                    owner, op_id,
                )

    async def job_flush_model_batches(self, owner: str, session_id: str) -> int:
        """Make pending batches for a session claimable when its conversation closes."""
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET payload = jsonb_set(payload, '{flush}', 'true'::jsonb, true) "
                "WHERE owner = $1 AND kind = 'extract_model' AND session_id = $2 "
                "AND status = 'pending'",
                owner, session_id,
            )
        return _count(status)

    async def job_claim(self, kind: str, worker: str, limit: int, *, lease_s: float,
                        min_age_s: float = 0.0, max_attempts: int = JOB_MAX_ATTEMPTS,
                        session_ordered: bool = False,
                        defer_backfill: bool = False,
                        ready_model_batch: bool = False) -> list[tuple[int, int, dict]]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE durable_job SET status = 'failed', claimed_by = NULL, lease_expires_at = NULL "
                    "WHERE kind = $1 AND attempts >= $2 AND (status IN ('pending', 'retryable') "
                    "OR (status = 'leased' AND lease_expires_at < now()))", kind, int(max_attempts))
                rows = await conn.fetch(
                    f"""
                    UPDATE durable_job j
                       SET status = 'leased', claimed_by = $2,
                           lease_expires_at = now() + make_interval(secs => $3),
                           fence = j.fence + 1, attempts = j.attempts + 1
                     WHERE j.id IN (
                           SELECT c.id FROM durable_job c
                            WHERE c.kind = $1 AND c.attempts < $5
                              AND (c.status = 'pending'
                                   OR (c.status = 'retryable' AND c.next_attempt_at <= now())
                                   OR (c.status = 'leased' AND c.lease_expires_at < now()))
                              AND c.created_at < now() - make_interval(secs => $4)
                              AND (NOT $9::boolean OR c.kind <> 'extract_model' OR
                                   coalesce(jsonb_array_length(c.payload->'span_ids'), 0) >= $10 OR
                                   c.payload->>'flush' = 'true' OR
                                   c.created_at < now() - make_interval(secs => $11) OR
                                   coalesce((c.payload->>'batch_started_at')::timestamptz,
                                            c.created_at) < now() - make_interval(secs => $12))
                              AND (NOT $6::boolean OR NOT EXISTS (
                                    SELECT 1 FROM durable_job e
                                     WHERE e.owner = c.owner AND e.kind = c.kind
                                       AND e.session_id = c.session_id AND e.status IN {_LIVE}
                                       AND (e.event_time, e.id) < (c.event_time, c.id)))
                              AND (NOT $8::boolean OR c.priority = 0 OR NOT EXISTS (
                                    SELECT 1 FROM durable_job l
                                     WHERE l.kind = c.kind AND l.priority = 0
                                       AND l.status IN {_LIVE}))
                            ORDER BY c.priority, c.event_time NULLS LAST, c.id
                            LIMIT $7
                            FOR UPDATE SKIP LOCKED)
                    RETURNING j.id, j.fence, j.payload
                    """,
                    kind, worker, float(lease_s), float(min_age_s), int(max_attempts),
                    bool(session_ordered), int(limit), bool(defer_backfill),
                    bool(ready_model_batch), MODEL_BATCH_SIZE,
                    MODEL_BATCH_IDLE_S, MODEL_BATCH_MAX_WAIT_S)
        return [(int(r["id"]), int(r["fence"]), _payload(r["payload"])) for r in rows]

    async def job_record_model_call(self, job_id: int, fence: int) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET payload = jsonb_set(payload, '{usage,calls}', "
                "to_jsonb(coalesce((payload->'usage'->>'calls')::int, 0) + 1), true) "
                "WHERE id = $1 AND fence = $2 AND status = 'leased'",
                int(job_id), int(fence),
            )
        return _count(status) == 1

    async def job_record_model_tokens(self, job_id: int, fence: int, prompt_tokens: Optional[int],
                                      completion_tokens: Optional[int]) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM durable_job WHERE id = $1 AND fence = $2 "
                    "AND status = 'leased' FOR UPDATE",
                    int(job_id), int(fence),
                )
                if exists is None:
                    return False
                for path, value in (
                    (["usage", "prompt_tokens"], prompt_tokens),
                    (["usage", "completion_tokens"], completion_tokens),
                ):
                    if value is not None:
                        await conn.execute(
                            "UPDATE durable_job SET payload = jsonb_set(payload, $3::text[], "
                            "to_jsonb(coalesce((payload #>> $3::text[])::bigint, 0) + $4), true) "
                            "WHERE id = $1 AND fence = $2 AND status = 'leased'",
                            int(job_id), int(fence), path, int(value),
                        )
        return True

    async def model_extraction_usage(self) -> dict[str, Optional[int | float]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) FILTER (WHERE status = 'done')::int AS batches_completed, "
                "coalesce(sum((payload->'usage'->>'calls')::int), 0)::int AS calls, "
                "sum((payload->'usage'->>'prompt_tokens')::bigint)::bigint AS prompt_tokens, "
                "sum((payload->'usage'->>'completion_tokens')::bigint)::bigint AS completion_tokens, "
                "coalesce(sum((payload->'usage'->>'calls')::int) FILTER "
                "(WHERE status = 'done'), 0)::int AS completed_calls, "
                "sum((payload->'usage'->>'prompt_tokens')::bigint) FILTER "
                "(WHERE status = 'done')::bigint AS completed_prompt_tokens, "
                "sum((payload->'usage'->>'completion_tokens')::bigint) FILTER "
                "(WHERE status = 'done')::bigint AS completed_completion_tokens "
                "FROM durable_job WHERE kind = 'extract_model'"
            )
        prompt = int(row["prompt_tokens"]) if row["prompt_tokens"] is not None else None
        completion = (
            int(row["completion_tokens"]) if row["completion_tokens"] is not None else None
        )
        completed_prompt = (
            int(row["completed_prompt_tokens"])
            if row["completed_prompt_tokens"] is not None else None
        )
        completed_completion = (
            int(row["completed_completion_tokens"])
            if row["completed_completion_tokens"] is not None else None
        )
        batches = int(row["batches_completed"] or 0)
        return {
            "batches_completed": batches,
            "calls": int(row["calls"] or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion if prompt is not None and completion is not None else None,
            "calls_per_completed_batch": int(row["completed_calls"] or 0) / batches if batches else None,
            "prompt_tokens_per_completed_batch": (
                completed_prompt / batches if completed_prompt is not None and batches else None
            ),
            "completion_tokens_per_completed_batch": (
                completed_completion / batches if completed_completion is not None and batches else None
            ),
        }

    async def job_heartbeat(self, job_id: int, fence: int, lease_s: float) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET lease_expires_at = now() + make_interval(secs => $3) "
                "WHERE id = $1 AND fence = $2 AND status = 'leased'", int(job_id), int(fence), float(lease_s))
        return _count(status) == 1

    async def job_done(self, job_id: int, fence: int, *, delete: bool = False) -> bool:
        async with self.pool.acquire() as conn:
            if delete:
                status = await conn.execute("DELETE FROM durable_job WHERE id = $1 AND fence = $2",
                                            int(job_id), int(fence))
            else:
                status = await conn.execute(
                    "UPDATE durable_job SET status = 'done', claimed_by = NULL, lease_expires_at = NULL "
                    "WHERE id = $1 AND fence = $2 AND status = 'leased'", int(job_id), int(fence))
        return _count(status) == 1

    async def job_release(self, job_id: int, fence: int, error: Optional[str] = None) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL, "
                "last_error = coalesce($3, last_error) WHERE id = $1 AND fence = $2 AND status = 'leased'",
                int(job_id), int(fence), error)
        return _count(status) == 1

    async def job_retry(self, job_id: int, fence: int, error: str) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                f"UPDATE durable_job SET status = CASE WHEN attempts >= $4 THEN 'failed' ELSE 'retryable' END, "
                f"next_attempt_at = now() + make_interval(secs => {_BACKOFF_SQL}), claimed_by = NULL, "
                f"lease_expires_at = NULL, last_error = $3 "
                f"WHERE id = $1 AND fence = $2 AND status = 'leased' RETURNING status",
                int(job_id), int(fence), error, JOB_MAX_ATTEMPTS)
        return row or "stale"

    async def job_release_worker(self, worker: str) -> int:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL "
                "WHERE claimed_by = $1 AND status = 'leased'", worker)
        return _count(status)

    async def job_counts(self) -> dict[str, dict[str, int]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT kind, status, count(*)::int AS n FROM durable_job GROUP BY 1, 2")
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
        return out

    # -- capture facade (Phase 0 signatures) -------------------------------------

    async def outbox_add(self, agent_identity: str, item: Dict[str, Any], claimed_by: str, op_id: str):
        return await self.job_add(agent_identity, "capture", op_id, item, claimed_by=claimed_by,
                                  lease_s=CAPTURE_LEASE_S)

    async def outbox_counts(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT count(*) FILTER (WHERE status IN {_LIVE} AND attempts < $1)::int AS pending, "
                f"count(*) FILTER (WHERE status = 'failed' OR (status IN {_LIVE} AND attempts >= $1))::int "
                f"AS parked FROM durable_job WHERE kind = 'capture'", JOB_MAX_ATTEMPTS)
        return {"pending": int(row["pending"]), "parked": int(row["parked"])}

    async def outbox_done(self, outbox_id: int, fence: int) -> bool:
        return await self.job_done(outbox_id, fence, delete=True)

    async def outbox_unclaim(self, outbox_id: int, fence: int, error: Optional[str] = None) -> bool:
        return await self.job_release(outbox_id, fence, error)

    async def outbox_release(self, claimed_by: str) -> int:
        return await self.job_release_worker(claimed_by)

    async def outbox_claim_stale(self, claimed_by: str, limit: int, *, min_age_s: float = 30.0,
                                 stale_claim_s: float = CAPTURE_LEASE_S,
                                 max_attempts: int = JOB_MAX_ATTEMPTS) -> list[tuple[int, int, dict]]:
        return await self.job_claim("capture", claimed_by, limit, lease_s=stale_claim_s,
                                    min_age_s=min_age_s, max_attempts=max_attempts)
