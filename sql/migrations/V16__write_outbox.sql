-- V16__write_outbox.sql — durable write intake for the provider drain.
-- sync_turn / on_memory_write record each write here before the in-memory
-- drain runs it. The drain deletes a row once its turn or memory row exists.
-- Rows left by a full queue, a shutdown, or a crash are replayed on the next
-- start. claimed_by / claimed_at keep two processes (gateway and a CLI
-- one-shot) from replaying the same row; attempts parks poison rows.
-- Idempotent: safe to re-run.

SET search_path = public;

CREATE TABLE IF NOT EXISTS write_outbox (
    id BIGSERIAL PRIMARY KEY,
    agent_identity TEXT NOT NULL,
    item JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by TEXT,
    claimed_at TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_write_outbox_created ON write_outbox (created_at);

-- Replay dedupe: a turn written from an outbox row carries its id in metadata.
CREATE INDEX IF NOT EXISTS idx_conversations_outbox_id
    ON conversations ((metadata->>'outbox_id'))
    WHERE metadata ? 'outbox_id';
