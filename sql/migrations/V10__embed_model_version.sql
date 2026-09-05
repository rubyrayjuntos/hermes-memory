-- V10__embed_model_version.sql — stamp which embedding model wrote each vector.
-- Idempotent. Existing rows stay NULL until rewritten.

SET search_path = public;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embed_model TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embed_dim INT;
ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS embed_model TEXT;
ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS embed_dim INT;
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS embed_model TEXT;
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS embed_dim INT;
