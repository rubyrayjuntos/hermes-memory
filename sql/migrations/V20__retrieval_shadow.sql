-- V20__retrieval_shadow.sql — hashed-query retrieval log for vector vs hybrid
-- comparison (no query text). Idempotent: safe to re-run.

SET search_path = public;
CREATE TABLE IF NOT EXISTS retrieval_shadow (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    query_key BYTEA NOT NULL,
    vector_ids JSONB NOT NULL,
    hybrid_ids JSONB NOT NULL,
    latency_ms JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (octet_length(query_key) = 16)
);
CREATE INDEX IF NOT EXISTS idx_retrieval_shadow_owner_created
    ON retrieval_shadow (owner, created_at);
