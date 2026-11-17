-- V21__retrieval_indexes.sql — indexes for owner-filtered claim retrieval.
-- Idempotent: safe to re-run.

SET search_path = public;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_claims_current_fact_vec_hnsw
    ON claims USING hnsw (fact_vec vector_cosine_ops)
    WHERE status = 'grounded' AND valid_to IS NULL AND fact_vec IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entity_name_norm_trgm
    ON entity USING gin (name_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entity_alias_surface_norm_trgm
    ON entity_alias USING gin (surface_norm gin_trgm_ops)
    WHERE state = 'live';
