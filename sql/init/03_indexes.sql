-- 03_indexes.sql — authoritative index DDL from plan §4. Idempotent via IF NOT EXISTS.
-- All objects fully qualified; no search_path needed.

-- Relational
CREATE INDEX IF NOT EXISTS idx_memory_entries_embedding
    ON memory_entries USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_mcn_vertex
    ON memory_chunk_nodes (vertex_id, graph_name);
CREATE INDEX IF NOT EXISTS idx_mcn_source_chunk
    ON memory_chunk_nodes (source, chunk_id);

CREATE UNIQUE INDEX IF NOT EXISTS memory_chunk_nodes_passport_uq
    ON memory_chunk_nodes (chunk_id, source, noun_id) WHERE noun_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS memory_chunk_nodes_flower_uq
    ON memory_chunk_nodes (chunk_id, source, vertex_id) WHERE noun_id IS NULL;

CREATE INDEX IF NOT EXISTS semantic_edge_src_vec_hnsw
    ON semantic_edge USING hnsw (e_src_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- conversations: dead-letter + queue partial indexes
CREATE INDEX IF NOT EXISTS idx_conversations_dead_letter
    ON conversations (ts) WHERE processing_attempts >= 5;
CREATE INDEX IF NOT EXISTS idx_conversations_queue
    ON conversations (ts) WHERE processed_at IS NULL
    AND content IS NOT NULL AND length(content) > 20;
CREATE INDEX IF NOT EXISTS idx_conversations_relations_queue
    ON conversations (relations_processed_at) WHERE relations_processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_conversations_embedding
    ON conversations USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- librarian file-path expression index
CREATE INDEX IF NOT EXISTS idx_librarian_file_path
    ON memory_entries (agent_identity, ((metadata->>'file_path')))
    WHERE agent_identity = 'librarian';

-- AGE label indexes. Per-label BTREE(id) + GIN(properties ag_catalog.gin_agtype_ops);
-- edges additionally BTREE(start_id) + BTREE(end_id).
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
