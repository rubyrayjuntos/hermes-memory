-- V15__claim_live_key.sql — one live row per proposition.
-- Depends on V12/V13 (same release, never applied live before this chain).
-- The partial unique key includes polarity: opposite-polarity rows coexist
-- live until a repaired uptake retracts one side. Unknown-validity rows
-- (abstained contradictions) bypass the index entirely.
-- seen_count is reinforcement, not truth: it never keeps a retracted row
-- alive (retracted rows leave the live set; the index only covers live).

SET search_path = public;

ALTER TABLE claims ADD COLUMN IF NOT EXISTS seen_count INT NOT NULL DEFAULT 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_live_key ON claims
    (subject_canon, verb, object_canon, polarity)
    WHERE valid = 'live';
