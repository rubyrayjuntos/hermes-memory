-- V14__claims_status.sql — catch-up flag for the reception overlay.
-- Depends on V12 (same release). Existing rows are grandfathered to 'none':
-- no history backfill (claims stay zero until a live user span asserts).
-- New rows default 'pending' at insert; the reception stage stamps
-- ready|none|failed. A SIGKILL between insert_turn and the stage leaves
-- 'pending', which catch_up_reception reaps on drain-task start.

SET search_path = public;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS claims_status TEXT;

-- Grandfather history: overlay was never attempted for these rows.
UPDATE conversations SET claims_status = 'none' WHERE claims_status IS NULL;

ALTER TABLE conversations ALTER COLUMN claims_status SET DEFAULT 'pending';
ALTER TABLE conversations ALTER COLUMN claims_status SET NOT NULL;

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_claims_status_chk;
ALTER TABLE conversations ADD CONSTRAINT conversations_claims_status_chk
    CHECK (claims_status IN ('pending', 'ready', 'none', 'failed'));
