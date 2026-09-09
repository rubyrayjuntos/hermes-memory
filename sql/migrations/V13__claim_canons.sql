-- V13__claim_canons.sql — canonical subject/object on claims.
-- Depends on V12 (same release; V12 was never applied to a live DB before V13).
-- canon = live-alias hit, else deterministic normalization. Never null, never
-- empty. Matching resolves through the CURRENT map on both sides at retract
-- time (aliases are retroactive); these columns are the write-time cache plus
-- the indexed path for claim-aware recall.

SET search_path = public;

ALTER TABLE claims ADD COLUMN IF NOT EXISTS subject_canon TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS object_canon TEXT NOT NULL DEFAULT '';

-- Belt-and-braces for any pre-V13 rows (expected: none). Python
-- normalize_surface remains the canonical normalizer for new writes.
UPDATE claims SET subject_canon =
    regexp_replace(lower(subject), '[^a-z0-9 ]', '', 'g')
    WHERE subject_canon = '';
UPDATE claims SET object_canon =
    regexp_replace(lower(object), '[^a-z0-9 ]', '', 'g')
    WHERE object_canon = '';

CREATE INDEX IF NOT EXISTS idx_claims_live_canon
    ON claims (subject_canon, verb) WHERE valid = 'live';
