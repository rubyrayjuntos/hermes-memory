-- V9__noun_passport_semantic_edge.sql — conversation manifold schema (noun, passports, semantic_edge)
-- Idempotent. No Concept backfill. No generated midpoint column on semantic_edge.

SET search_path = public;

CREATE TABLE IF NOT EXISTS noun (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    type TEXT,
    ag_vertex_id BIGINT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semantic_edge (
    id SERIAL PRIMARY KEY,
    src_noun INT NOT NULL REFERENCES noun(id) ON DELETE CASCADE,
    tgt_noun INT NOT NULL REFERENCES noun(id) ON DELETE CASCADE,
    verb_type TEXT NOT NULL,
    e_src_vec vector(768) NOT NULL,
    e_tgt_vec vector(768) NOT NULL,
    magnitude FLOAT NOT NULL CHECK (magnitude > 0 AND magnitude <= 8),
    polarity SMALLINT NOT NULL DEFAULT 1 CHECK (polarity IN (-1, 1)),
    last_active_turn BIGINT,
    last_active_ts TIMESTAMPTZ,
    provenance_turns BIGINT[] NOT NULL DEFAULT '{}',
    UNIQUE (src_noun, tgt_noun, verb_type)
);

CREATE INDEX IF NOT EXISTS semantic_edge_src_vec_hnsw
    ON semantic_edge USING hnsw (e_src_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS noun_id INT REFERENCES noun(id) ON DELETE CASCADE;
ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS turn_id BIGINT;
ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS conf FLOAT CHECK (conf >= 0 AND conf <= 1);

ALTER TABLE memory_chunk_nodes DROP CONSTRAINT IF EXISTS memory_chunk_nodes_pkey;
ALTER TABLE memory_chunk_nodes ALTER COLUMN vertex_id DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS memory_chunk_nodes_passport_uq
    ON memory_chunk_nodes (chunk_id, source, noun_id) WHERE noun_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS memory_chunk_nodes_flower_uq
    ON memory_chunk_nodes (chunk_id, source, vertex_id) WHERE noun_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_conversations_embedding
    ON conversations USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
