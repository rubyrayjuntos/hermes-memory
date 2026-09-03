-- V8__bridge_canonical_alias.sql — canonical bridge keys + doc_chunks search (C2)
-- 1. Backfill canonical chunk_id = memory_entries.id::text for every legacy mem_ bridge.
--    Keeps the legacy 'mem_' row as alias during the migration window so both old and
--    new code paths resolve. Idempotent via ON CONFLICT DO NOTHING.
-- 2. Ensure doc_chunks ANN is indexed for vector_search (ingest doc_chunks were never indexed).
-- 3. Fresh installs already create canonical rows via ingest.py; this covers upgraded DBs.

SET search_path = public;

-- HNSW for doc_chunks so vector_search UNION ALL doc_chunks is not a seqscan
CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
    ON doc_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Backfill canonical bridges from legacy mem_ rows (bounded; does not delete alias)
INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name)
SELECT e.id::text AS chunk_id, b.source, b.vertex_id, b.graph_name
  FROM memory_chunk_nodes b
  JOIN memory_entries e ON b.chunk_id = 'mem_' || e.id::text
  ON CONFLICT (chunk_id, source, vertex_id) DO NOTHING;

-- Also ensure there is no orphan canonical without alias? Optional reverse alias not needed,
-- but keep symmetry: if someone manually inserted canonical before, ensure alias exists too
-- (harmless duplicate guard).
INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name)
SELECT 'mem_' || e.id::text AS chunk_id, b.source, b.vertex_id, b.graph_name
  FROM memory_chunk_nodes b
  JOIN memory_entries e ON b.chunk_id = e.id::text
 WHERE NOT EXISTS (
   SELECT 1 FROM memory_chunk_nodes c
    WHERE c.chunk_id = 'mem_' || e.id::text
      AND c.vertex_id = b.vertex_id
      AND c.source = b.source
 )
  ON CONFLICT (chunk_id, source, vertex_id) DO NOTHING;
