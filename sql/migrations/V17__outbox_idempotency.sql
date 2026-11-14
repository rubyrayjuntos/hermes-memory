-- V17__outbox_idempotency.sql — database-enforced exactly-once for captured writes.
-- Every captured write carries an op_id. One outbox row and one conversation row
-- per op_id, enforced by unique indexes and written with ON CONFLICT, so a record
-- that commits after a client timeout, a replay racing the live path, or a worker
-- whose lease expired cannot store a write twice. fence increments on every claim;
-- settling a row requires the current fence, so a stale worker cannot delete or
-- hand back a row another worker now holds. Idempotent: safe to re-run.

SET search_path = public;

ALTER TABLE write_outbox ADD COLUMN IF NOT EXISTS op_id TEXT;
ALTER TABLE write_outbox ADD COLUMN IF NOT EXISTS fence BIGINT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_write_outbox_op_id
    ON write_outbox (op_id) WHERE op_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_op_id
    ON conversations ((metadata->>'op_id')) WHERE metadata ? 'op_id';

-- Superseded by uq_conversations_op_id (V16 wrote outbox_id; V17 writes op_id).
DROP INDEX IF EXISTS idx_conversations_outbox_id;
