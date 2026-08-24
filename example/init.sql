-- hermes-memory: Postgres schema for hybrid-age memory provider
-- Run this once after creating the database.

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';

-- Knowledge graph
SELECT create_graph('hermes_knowledge');

-- Conversation storage with vector support
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_identity TEXT DEFAULT 'default',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TIMESTAMPTZ DEFAULT now(),
    embedding vector(768),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conversations_embedding
    ON conversations USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Memory entries with vector support
CREATE TABLE IF NOT EXISTS memory_entries (
    id SERIAL PRIMARY KEY,
    agent_identity TEXT DEFAULT 'default',
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(agent_identity, target, content)
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_embedding
    ON memory_entries USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Bridge table: vector chunks ↔ AGE graph vertices
CREATE TABLE IF NOT EXISTS memory_chunk_nodes (
    chunk_id TEXT NOT NULL,
    source TEXT NOT NULL,
    vertex_id BIGINT NOT NULL,
    graph_name TEXT NOT NULL DEFAULT 'hermes_knowledge',
    PRIMARY KEY (chunk_id, source, vertex_id)
);

-- Pre-declare graph labels
SELECT create_vlabel('hermes_knowledge', 'Person');
SELECT create_vlabel('hermes_knowledge', 'Project');
SELECT create_vlabel('hermes_knowledge', 'Technology');
SELECT create_vlabel('hermes_knowledge', 'Organization');
SELECT create_vlabel('hermes_knowledge', 'Concept');
SELECT create_vlabel('hermes_knowledge', 'Domain');
SELECT create_vlabel('hermes_knowledge', 'Skill');
SELECT create_vlabel('hermes_knowledge', 'Tool');
SELECT create_elabel('hermes_knowledge', 'RELATED_TO');
SELECT create_elabel('hermes_knowledge', 'USES');
SELECT create_elabel('hermes_knowledge', 'BUILT_WITH');
SELECT create_elabel('hermes_knowledge', 'DEPENDS_ON');
SELECT create_elabel('hermes_knowledge', 'WORKS_ON');
SELECT create_elabel('hermes_knowledge', 'CO_MENTIONED');
