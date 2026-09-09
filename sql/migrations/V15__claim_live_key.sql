-- V15__claim_live_key.sql — one live row per proposition.
-- Depends on V12/V13 (same release, never applied live before this chain).
-- The partial unique key includes polarity: opposite-polarity rows coexist
-- live until a repaired uptake retracts one side. Unknown-validity rows
-- (abstained contradictions) bypass the index entirely.
-- seen_count is reinforcement, not truth: it never keeps a retracted row
-- alive (retracted rows leave the live set; the index only covers live).

SET search_path = public;

ALTER TABLE claims ADD COLUMN IF NOT EXISTS seen_count INT NOT NULL DEFAULT 1;

-- Collapse pre-existing duplicate live rows for this key. V12–V14 inserted
-- claims unconditionally, so any database that received the same assertion
-- more than once has duplicate live rows that would abort the unique index
-- below. Keep the oldest live row per key (lowest claim_id), roll its
-- seen_count up to the group total, and retract the rest. Deterministic and
-- idempotent: a database with no duplicates is unchanged.
WITH live_dupes AS (
    SELECT claim_id,
           subject_canon, verb, object_canon, polarity,
           seen_count,
           MIN(claim_id) OVER w AS survivor_id,
           SUM(seen_count) OVER w AS group_seen,
           COUNT(*) OVER w AS group_size
    FROM claims
    WHERE valid = 'live'
    WINDOW w AS (PARTITION BY subject_canon, verb, object_canon, polarity)
),
survivor_updates AS (
    UPDATE claims c
    SET seen_count = ld.group_seen
    FROM live_dupes ld
    WHERE c.claim_id = ld.survivor_id
      AND ld.group_size > 1
      AND c.claim_id = ld.claim_id
    RETURNING c.claim_id
)
UPDATE claims c
SET valid = 'retracted',
    superseded_by = ld.survivor_id
FROM live_dupes ld
WHERE c.claim_id = ld.claim_id
  AND ld.group_size > 1
  AND ld.claim_id <> ld.survivor_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_live_key ON claims
    (subject_canon, verb, object_canon, polarity)
    WHERE valid = 'live';
