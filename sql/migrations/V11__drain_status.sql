-- V11__drain_status.sql — persist C–F drain outcome on the conversation row.
-- Idempotent. Historical rows stay NULL (never stamped).
-- Values: complete | embed_null | graph_degraded
-- This is not the V9 unpassported gap (passport anti-join). Do not conflate.

SET search_path = public;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS drain_status TEXT;

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_drain_status_chk;
ALTER TABLE conversations ADD CONSTRAINT conversations_drain_status_chk
    CHECK (
        drain_status IS NULL
        OR drain_status IN ('complete', 'embed_null', 'graph_degraded')
    );

CREATE INDEX IF NOT EXISTS conversations_drain_status_idx
    ON conversations (drain_status)
    WHERE drain_status IS NOT NULL;
