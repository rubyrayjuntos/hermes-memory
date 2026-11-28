-- V24__noun_label_slug_index.sql — index-backed label lookup (issue #123)
--
-- fetch_noun_labels() used to SELECT every row in `noun` on every turn write
-- so extract_nouns() could resolve a candidate surface to an existing
-- canonical label. That scales with the whole table, not the current turn.
-- `slug` lets the per-turn lookup filter with `WHERE slug = ANY($1)` against
-- an index instead of reading the whole table.
--
-- Plain column, NOT generated. Store.upsert_noun() computes the authoritative
-- value in Python (extract_nouns._slug()) on every write/re-write going
-- forward, because Python's str.lower() and Postgres's lower() do not always
-- agree on exotic Unicode case folding (e.g. Turkish dotted capital I,
-- U+0130, lowers to a two-codepoint 'i' + combining-dot-above in Python but
-- to a plain ASCII 'i' in Postgres) -- a generated column computed in SQL
-- would silently diverge from the Python-side slug Store.fetch_noun_labels_
-- for_slugs() is queried with. The UPDATE below is a best-effort backfill
-- for rows that already existed before this migration ran, using the same
-- rule as _slug() for the overwhelming common case (plain ASCII and ordinary
-- accented Unicode); it can be exactly wrong for the same rare multi-
-- codepoint-expansion edge cases Python and Postgres disagree on, which only
-- matters for legacy rows nobody has re-touched since -- any noun upserted
-- after this migration gets the Python-exact value via upsert_noun(), which
-- self-heals a stale legacy slug on its next touch (ON CONFLICT DO UPDATE).
--
-- Not UNIQUE: distinct labels (e.g. "AT&T" vs "AT T") can share a slug.

SET search_path = public;

ALTER TABLE noun ADD COLUMN IF NOT EXISTS slug TEXT;

UPDATE noun SET slug = regexp_replace(
    regexp_replace(lower(trim(label)), '[^a-z0-9]+', '-', 'g'),
    '(^-+)|(-+$)', '', 'g'
) WHERE slug IS NULL;

CREATE INDEX IF NOT EXISTS idx_noun_slug ON noun (slug);
