-- V19__durable_job.sql — one lease-and-fence job lifecycle for capture and
-- extraction (graph layer spec v0.5.1 §5.1). Capture moves off write_outbox:
-- pending rows are copied here; write_outbox stays (empty) until Phase 4 so
-- an older release can still start. Idempotent: safe to re-run.

SET search_path = public;

CREATE TABLE IF NOT EXISTS durable_job (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('capture', 'extract_rules', 'extract_model')),
    op_id TEXT NOT NULL,
    session_id TEXT,
    event_time TIMESTAMPTZ,
    priority INT NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'retryable', 'done', 'failed')),
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    fence BIGINT NOT NULL DEFAULT 0,
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, kind, op_id)
);
CREATE INDEX IF NOT EXISTS idx_durable_job_session_order ON durable_job (owner, session_id, kind, event_time)
    WHERE status IN ('pending', 'leased', 'retryable');
CREATE INDEX IF NOT EXISTS idx_durable_job_claimable ON durable_job (kind, priority, event_time, id)
    WHERE status IN ('pending', 'leased', 'retryable');

INSERT INTO durable_job (owner, kind, op_id, payload, status, attempts, last_error, created_at, event_time)
SELECT agent_identity, 'capture', coalesce(op_id, 'outbox:' || id::text), item,
       CASE WHEN attempts >= 5 THEN 'failed' ELSE 'pending' END, attempts, last_error, created_at, created_at
  FROM write_outbox
ON CONFLICT (owner, kind, op_id) DO NOTHING;
DELETE FROM write_outbox wo
 WHERE EXISTS (SELECT 1 FROM durable_job j WHERE j.kind = 'capture' AND j.owner = wo.agent_identity
                 AND j.op_id = coalesce(wo.op_id, 'outbox:' || wo.id::text));
