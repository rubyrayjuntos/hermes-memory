-- 006_fix_memory_entries_conflict_target.sql
-- Align memory_entries dedup constraint with store.py's ON CONFLICT target.
-- Production (migration 002 lineage) has a md5(content) expression index;
-- the provider upserts with ON CONFLICT (agent_identity, target, content),
-- which requires the plain UNIQUE from sql/init/02_schema.sql.
-- Safe: adding UNIQUE when no duplicates exist; dropping the redundant
-- md5 index afterwards. Idempotent.

-- 1. remove any duplicate rows that would block the new UNIQUE constraint
DELETE FROM memory_entries a
 USING memory_entries b
 WHERE a.id > b.id
   AND a.agent_identity = b.agent_identity
   AND a.target = b.target
   AND a.content = b.content;

-- 2. add the schema-canonical unique constraint if missing
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'memory_entries_agent_target_content_key'
           AND conrelid = 'public.memory_entries'::regclass
    ) THEN
        ALTER TABLE memory_entries
            ADD CONSTRAINT memory_entries_agent_target_content_key
            UNIQUE (agent_identity, target, content);
    END IF;
END $$;

-- 3. drop the now-redundant md5 expression index
DROP INDEX IF EXISTS memory_entries_unique_hash;

INSERT INTO schema_migrations (filename, applied_at)
VALUES ('006_fix_memory_entries_conflict_target.sql', now())
ON CONFLICT (filename) DO NOTHING;
