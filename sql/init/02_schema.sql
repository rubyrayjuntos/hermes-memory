-- 02_schema.sql — relational tables (plan §4). Idempotent via IF NOT EXISTS.
SET search_path = public;

CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_identity TEXT NOT NULL DEFAULT 'default',
    role TEXT NOT NULL,
    content TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding vector(768),
    metadata JSONB NOT NULL DEFAULT '{}',
    -- extractor-ready queue columns
    processed_at TIMESTAMPTZ,
    relations_processed_at TIMESTAMPTZ,
    processing_attempts INT NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_identity TEXT NOT NULL DEFAULT 'default',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id BIGSERIAL PRIMARY KEY,
    agent_identity TEXT NOT NULL DEFAULT 'default',
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS memory_entries_unique_hash
    ON public.memory_entries (agent_identity, target, md5(content));

-- Bridge: pgvector chunks <-> AGE graph vertices
CREATE TABLE IF NOT EXISTS memory_chunk_nodes (
    chunk_id TEXT NOT NULL,
    source TEXT NOT NULL,
    vertex_id BIGINT NOT NULL,
    graph_name TEXT NOT NULL DEFAULT 'hermes_knowledge',
    PRIMARY KEY (chunk_id, source, vertex_id)
);

CREATE TABLE IF NOT EXISTS doc_chunks (
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

CREATE TABLE IF NOT EXISTS librarian_runs (
    id BIGSERIAL PRIMARY KEY,
    agent_identity TEXT NOT NULL DEFAULT 'librarian',
    file_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS librarian_run_events (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES librarian_runs(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
