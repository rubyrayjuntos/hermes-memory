-- claim_evidence.speaker CHECK was declared inline in V18:167 (auto-named).
ALTER TABLE claim_evidence DROP CONSTRAINT IF EXISTS claim_evidence_speaker_check;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'claim_evidence'::regclass
       AND conname = 'claim_evidence_speaker_check_v2'
  ) THEN
    ALTER TABLE claim_evidence ADD CONSTRAINT claim_evidence_speaker_check_v2
      CHECK (speaker IN ('user','assistant','document'));
  END IF;
END $$;
ALTER TABLE claim_evidence ADD COLUMN IF NOT EXISTS seg_start INT;
ALTER TABLE claim_evidence ADD COLUMN IF NOT EXISTS seg_end   INT;  -- half-open offsets into stored conversations.content
