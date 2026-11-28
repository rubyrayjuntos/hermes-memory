-- V24__noun_label_slug_index.sql — index-backed label lookup (issue #123)
--
-- fetch_noun_labels() used to SELECT every row in `noun` on every turn write
-- so extract_nouns() could resolve a candidate surface to an existing
-- canonical label. That scales with the whole table, not the current turn.
-- `slug` lets the per-turn lookup filter with `WHERE slug = ANY($1)` against
-- an index instead of reading the whole table. Computed with the same rule
-- as extract_nouns._slug(): lowercase, collapse runs of non [a-z0-9] to a
-- single '-', then trim leading/trailing '-'. Not UNIQUE: distinct labels
-- (e.g. "AT&T" vs "AT T") can share a slug.

SET search_path = public;

ALTER TABLE noun ADD COLUMN IF NOT EXISTS slug TEXT
    GENERATED ALWAYS AS (
        regexp_replace(
            regexp_replace(lower(trim(label)), '[^a-z0-9]+', '-', 'g'),
            '(^-+)|(-+$)', '', 'g'
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_noun_slug ON noun (slug);
