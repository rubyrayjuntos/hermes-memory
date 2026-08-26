-- V1__vector_age.sql — baseline vector + AGE + graph + core schema + indexes
-- Idempotent: safe to re-apply (IF NOT EXISTS, exception-guarded graph/label creation).
-- Canonical source for docker init and file-based migrate.py runner.

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Migration history (created here so V1 is self-contained; runner also ensures it)
CREATE TABLE IF NOT EXISTS migration_history (
    version TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    checksum TEXT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    execution_time_ms INTEGER NOT NULL DEFAULT 0
);
-- Back-fill execution_time_ms if table pre-existed without the column
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'migration_history' AND column_name = 'execution_time_ms'
    ) THEN
        ALTER TABLE migration_history ADD COLUMN execution_time_ms INTEGER NOT NULL DEFAULT 0;
    END IF;
END $$;

-- Legacy table kept for backward compat (migrate.py migrates rows into migration_history)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Graph (idempotent guard: duplicate_object / duplicate_schema both ignored)
DO $$ BEGIN
    PERFORM create_graph('hermes_knowledge');
EXCEPTION WHEN duplicate_object THEN NULL;
            WHEN duplicate_schema THEN NULL;
            WHEN OTHERS THEN
                IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF;
END $$;

-- Vertex labels
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Person'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Project'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Technology'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Organization'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Concept'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Domain'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Skill'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Tool'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Repo'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'File'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Module'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Dependency'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Standard'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Session'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_vlabel('hermes_knowledge', 'Turn'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;

-- Edge labels
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'Uses'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'BuiltWith'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'WorksOn'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'PartOf'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'DependsOn'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'Implements'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'Imports'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'GovernedBy'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'Deprecates'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'Mentions'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'CoMentioned'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;
DO $$ BEGIN PERFORM create_elabel('hermes_knowledge', 'SemanticallyRelated'); EXCEPTION WHEN duplicate_object THEN NULL; WHEN OTHERS THEN IF SQLERRM LIKE '%already exists%' THEN NULL; ELSE RAISE; END IF; END $$;

SET search_path = public;

-- Core relational tables (IF NOT EXISTS)
CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_identity TEXT NOT NULL DEFAULT 'default',
    role TEXT NOT NULL,
    content TEXT,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding vector(768),
    metadata JSONB NOT NULL DEFAULT '{}',
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_identity, target, content)
);

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

-- Indexes (plan §4)
CREATE INDEX IF NOT EXISTS idx_memory_entries_embedding
    ON memory_entries USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_mcn_vertex
    ON memory_chunk_nodes (vertex_id, graph_name);
CREATE INDEX IF NOT EXISTS idx_mcn_source_chunk
    ON memory_chunk_nodes (source, chunk_id);

CREATE INDEX IF NOT EXISTS idx_conversations_dead_letter
    ON conversations (ts) WHERE processing_attempts >= 5;
CREATE INDEX IF NOT EXISTS idx_conversations_queue
    ON conversations (ts) WHERE processed_at IS NULL
    AND content IS NOT NULL AND length(content) > 20;
CREATE INDEX IF NOT EXISTS idx_conversations_relations_queue
    ON conversations (relations_processed_at) WHERE relations_processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_librarian_file_path
    ON memory_entries (agent_identity, ((metadata->>'file_path')))
    WHERE agent_identity = 'librarian';

-- AGE label indexes
DO $$
DECLARE
    v_label TEXT;
    e_label TEXT;
BEGIN
    FOREACH v_label IN ARRAY ARRAY[
        'Person','Project','Technology','Organization','Concept','Domain','Skill',
        'Tool','Repo','File','Module','Dependency','Standard','Session','Turn'
    ] LOOP
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON hermes_knowledge.%I USING BTREE (id)',
            'idx_' || lower(v_label) || '_id', v_label);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON hermes_knowledge.%I USING GIN (properties ag_catalog.gin_agtype_ops)',
            'idx_' || lower(v_label) || '_props', v_label);
    END LOOP;

    FOREACH e_label IN ARRAY ARRAY[
        'Uses','BuiltWith','WorksOn','PartOf','DependsOn','Implements','Imports',
        'GovernedBy','Deprecates','Mentions','CoMentioned','SemanticallyRelated'
    ] LOOP
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON hermes_knowledge.%I USING BTREE (id)',
            'idx_' || lower(e_label) || '_id', e_label);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON hermes_knowledge.%I USING GIN (properties ag_catalog.gin_agtype_ops)',
            'idx_' || lower(e_label) || '_props', e_label);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON hermes_knowledge.%I USING BTREE (start_id)',
            'idx_' || lower(e_label) || '_start', e_label);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON hermes_knowledge.%I USING BTREE (end_id)',
            'idx_' || lower(e_label) || '_end', e_label);
    END LOOP;
END
$$;
