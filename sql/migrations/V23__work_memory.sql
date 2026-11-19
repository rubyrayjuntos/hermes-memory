-- V23__work_memory.sql — Spec 5: work_item / work_artifact / work_event, a
-- separate record type for "what was done, decided, tried and failed."
-- Never written to claims/claim_evidence. Idempotent: safe to re-run.

SET search_path = public;

CREATE TABLE IF NOT EXISTS work_event (   -- tool-result digests; no raw output
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL,
  span_id BIGINT,
  source_event_id TEXT NOT NULL,          -- stable tool-call/event id for replay deduplication
  tool TEXT NOT NULL,
  status_code INT,
  refs JSONB NOT NULL DEFAULT '{}',        -- allowlisted, size-capped paths/refs/counts only
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner, id),
  UNIQUE (owner, session_id, source_event_id),
  FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (span_id)
);

CREATE TABLE IF NOT EXISTS work_item (
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,                -- normalized task/topic key
  source_op_id TEXT NOT NULL,              -- stable extract_work op_id; retry idempotency
  ask_summary TEXT NOT NULL,
  outcome_summary TEXT,
  status TEXT NOT NULL CHECK (status IN ('done','failed','abandoned','unknown','retracted')),
  reason TEXT,
  ask_span_id BIGINT NOT NULL,
  outcome_span_id BIGINT,
  reason_span_id BIGINT,
  reason_event_id BIGINT,
  superseded_by BIGINT,
  extractor_version TEXT NOT NULL,
  embedding vector(768),                  -- this provider's fixed embedding dimension
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner, id),
  UNIQUE (owner, topic_key, id),
  UNIQUE (owner, source_op_id),
  FOREIGN KEY (owner, ask_span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
  FOREIGN KEY (owner, outcome_span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (outcome_span_id),
  FOREIGN KEY (owner, reason_span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (reason_span_id),
  FOREIGN KEY (owner, reason_event_id) REFERENCES work_event (owner, id),
  FOREIGN KEY (owner, superseded_by) REFERENCES work_item (owner, id),
  CONSTRAINT work_item_reason_required CHECK (
    status NOT IN ('failed','abandoned') OR
    (reason IS NOT NULL AND (reason_span_id IS NOT NULL OR reason_event_id IS NOT NULL))
  )
);

CREATE TABLE IF NOT EXISTS work_item_current (
  owner TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  work_item_id BIGINT NOT NULL,
  PRIMARY KEY (owner, topic_key),
  FOREIGN KEY (owner, topic_key, work_item_id) REFERENCES work_item (owner, topic_key, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_artifact (
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  work_item_id BIGINT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('file','branch','commit','pr','eval')),
  ref TEXT NOT NULL,                      -- repository-relative path / branch / sha / PR number / eval identifier
  repo TEXT,
  exists_now BOOLEAN,
  verified_at TIMESTAMPTZ,
  stale BOOLEAN NOT NULL DEFAULT false,
  UNIQUE (owner, work_item_id, kind, ref),
  FOREIGN KEY (owner, work_item_id) REFERENCES work_item (owner, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_work_event_span ON work_event (owner, span_id);
CREATE INDEX IF NOT EXISTS idx_work_item_topic ON work_item (owner, topic_key, created_at);
CREATE INDEX IF NOT EXISTS idx_work_artifact_item ON work_artifact (owner, work_item_id);

-- durable_job.kind CHECK is inline in V19:11 (auto-named): drop & re-add with 'extract_work'.
ALTER TABLE durable_job DROP CONSTRAINT IF EXISTS durable_job_kind_check;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'durable_job'::regclass
       AND conname = 'durable_job_kind_check_v2'
  ) THEN
    ALTER TABLE durable_job ADD CONSTRAINT durable_job_kind_check_v2
      CHECK (kind IN ('capture', 'extract_rules', 'extract_model', 'extract_work'));
  END IF;
END $$;
