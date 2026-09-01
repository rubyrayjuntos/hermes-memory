-- V7__canonical_md5_dedup.sql
-- Fix memory_entries dedup: btree UNIQUE on raw content exceeds 2704 bytes for large content.
-- Canonical per AGENTS.md: dedup is on (agent_identity, target, md5(content)).
-- Published as V7 so deployments that already applied the previous V6 (plain-content
-- UNIQUE) do not hit a checksum mismatch via scripts/migrate.py.

-- 1. dedup duplicate rows that would block either constraint (keep lowest id)
DELETE FROM memory_entries a
 USING memory_entries b
 WHERE a.id > b.id
   AND a.agent_identity = b.agent_identity
   AND a.target = b.target
   AND md5(a.content) = md5(b.content);

-- 2. drop the oversized plain-content uniques if they exist
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_entries_agent_target_content_key' AND conrelid = 'public.memory_entries'::regclass) THEN
        ALTER TABLE public.memory_entries DROP CONSTRAINT memory_entries_agent_target_content_key;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_entries_agent_identity_target_content_key' AND conrelid = 'public.memory_entries'::regclass) THEN
        ALTER TABLE public.memory_entries DROP CONSTRAINT memory_entries_agent_identity_target_content_key;
    END IF;
END $$;
-- Also drop plain unique indexes if they exist as indexes without constraint
DROP INDEX IF EXISTS public.memory_entries_agent_target_content_key;
DROP INDEX IF EXISTS public.memory_entries_agent_identity_target_content_key;

-- 3. ensure md5-based unique index exists
CREATE UNIQUE INDEX IF NOT EXISTS memory_entries_unique_hash
    ON public.memory_entries (agent_identity, target, md5(content));
