-- V2__librarian_chunks.sql — librarian chunk store + HNSW index
-- Idempotent via IF NOT EXISTS.

SET search_path = public;

-- librarian_chunks: codebase/doc chunks owned by the librarian agent.
-- Separate from doc_chunks so per-source HNSW tuning and retention can diverge.
CREATE TABLE IF NOT EXISTS librarian_chunks (
    id TEXT PRIMARY KEY,
    doc_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    file_path TEXT,
    ordinal INT NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    embedding vector(768),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, file_path, doc_hash, ordinal)
);

-- HNSW cosine index — matches memory_entries tuning (m=16, ef_construction=64)
CREATE INDEX IF NOT EXISTS idx_librarian_chunks_embedding
    ON librarian_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Source + file lookup
CREATE INDEX IF NOT EXISTS idx_librarian_chunks_source_file
    ON librarian_chunks (source, file_path);

-- Bridge entries for librarian_chunks -> graph (reuse memory_chunk_nodes)
-- Ensure bridge indexes exist (also created in V1; safe to re-create)
CREATE INDEX IF NOT EXISTS idx_mcn_vertex
    ON memory_chunk_nodes (vertex_id, graph_name);
CREATE INDEX IF NOT EXISTS idx_mcn_source_chunk
    ON memory_chunk_nodes (source, chunk_id);
