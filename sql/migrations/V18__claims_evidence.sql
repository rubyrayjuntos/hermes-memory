-- V18__claims_evidence.sql — owner-scoped entities, evidence and timelines
-- (graph layer spec v0.5.1 §4, §7.2 step 1). Claims become a projection of
-- claim_evidence; status replaces valid, which a trigger mirrors until Phase 4.
-- Legacy rows keep their pre-migration valid in legacy_valid and get a
-- provisional status; claims_migration.py (data_migrations 'claims_evidence_v1')
-- writes their evidence and rebuilds timelines. Idempotent: safe to re-run.

SET search_path = public;

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_owner_id ON conversations (agent_identity, id);

CREATE TABLE IF NOT EXISTS entity (
    owner TEXT NOT NULL,
    id BIGSERIAL NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'other'
        CHECK (type IN ('self', 'person', 'project', 'place', 'org', 'tool', 'concept', 'other')),
    name_vec vector(768),
    merged_into BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, id),
    UNIQUE (owner, name_norm, type),
    FOREIGN KEY (owner, merged_into) REFERENCES entity (owner, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_self ON entity (owner) WHERE type = 'self';
CREATE INDEX IF NOT EXISTS idx_entity_name_norm ON entity (owner, name_norm);

CREATE TABLE IF NOT EXISTS entity_alias (
    owner TEXT NOT NULL,
    surface_norm TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('equation', 'extractor', 'migration', 'manual')),
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retracted')),
    span_id BIGINT,
    event_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, surface_norm, entity_id),
    FOREIGN KEY (owner, entity_id) REFERENCES entity (owner, id),
    FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_alias_live ON entity_alias (owner, surface_norm) WHERE state = 'live';

CREATE TABLE IF NOT EXISTS turn_entity (
    owner TEXT NOT NULL,
    span_id BIGINT NOT NULL,
    entity_id BIGINT NOT NULL,
    surface TEXT NOT NULL,
    conf DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    PRIMARY KEY (owner, span_id, entity_id),
    FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
    FOREIGN KEY (owner, entity_id) REFERENCES entity (owner, id)
);
CREATE INDEX IF NOT EXISTS idx_turn_entity_entity ON turn_entity (owner, entity_id);

CREATE TABLE IF NOT EXISTS verb (
    verb TEXT PRIMARY KEY,
    functional BOOLEAN NOT NULL,
    phrase TEXT NOT NULL,
    neg_phrase TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS verb_synonym (
    phrase_norm TEXT PRIMARY KEY,
    verb TEXT NOT NULL REFERENCES verb (verb)
);
INSERT INTO verb (verb, functional, phrase, neg_phrase) VALUES
    ('is', false, 'is', 'is not'),
    ('uses', false, 'uses', 'does not use'),
    ('based_in', true, 'is based in', 'is not based in'),
    ('owned_by', true, 'is owned by', 'is not owned by'),
    ('works_for', true, 'works for', 'does not work for'),
    ('lives_in', true, 'lives in', 'does not live in'),
    ('role_is', true, 'has the role', 'does not have the role'),
    ('status_is', true, 'has the status', 'does not have the status'),
    ('works_on', false, 'works on', 'does not work on'),
    ('depends_on', false, 'depends on', 'does not depend on'),
    ('part_of', false, 'is part of', 'is not part of'),
    ('knows', false, 'knows', 'does not know'),
    ('likes', false, 'likes', 'does not like'),
    ('prefers', false, 'prefers', 'does not prefer')
ON CONFLICT (verb) DO NOTHING;
INSERT INTO verb_synonym (phrase_norm, verb) VALUES
    ('is', 'is'), ('uses', 'uses'), ('use', 'uses'),
    ('works for', 'works_for'), ('works at', 'works_for'), ('employed by', 'works_for'),
    ('is employed by', 'works_for'), ('lives in', 'lives_in'), ('based in', 'based_in'),
    ('is based in', 'based_in'), ('headquartered in', 'based_in'), ('owned by', 'owned_by'),
    ('is owned by', 'owned_by'), ('belongs to', 'owned_by'), ('role is', 'role_is'),
    ('status is', 'status_is'), ('works on', 'works_on'), ('depends on', 'depends_on'),
    ('part of', 'part_of'), ('is part of', 'part_of'), ('knows', 'knows'),
    ('likes', 'likes'), ('prefers', 'prefers')
ON CONFLICT (phrase_norm) DO NOTHING;

ALTER TABLE claims ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS subject_id BIGINT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS object_id BIGINT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS fact_text TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS fact_vec vector(768);
ALTER TABLE claims ADD COLUMN IF NOT EXISTS embed_model TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS legacy_valid TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_status_check') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_status_check
            CHECK (status IN ('grounded', 'unconfirmed', 'retracted', 'orphaned'));
    END IF;
END $$;

-- Freeze pre-migration truth once (new rows always carry an owner), then owner,
-- then a provisional status that matches valid until the data migration runs.
UPDATE claims SET legacy_valid = valid WHERE legacy_valid IS NULL AND owner IS NULL;
UPDATE claims c SET owner = cv.agent_identity
  FROM conversations cv WHERE cv.id = c.span_id AND c.owner IS NULL;
UPDATE claims SET status = CASE valid WHEN 'live' THEN 'grounded'
                                      WHEN 'retracted' THEN 'retracted'
                                      ELSE 'unconfirmed' END
 WHERE status IS NULL;

-- Provenance moves to claim_evidence; spans must be deletable.
ALTER TABLE claims ALTER COLUMN span_id DROP NOT NULL;
ALTER TABLE claims DROP CONSTRAINT IF EXISTS claims_span_id_fkey;

CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_owner_claim ON claims (owner, claim_id);
-- The global live key collides across owners; claims_current_key replaces it.
DROP INDEX IF EXISTS idx_claim_live_key;
CREATE UNIQUE INDEX IF NOT EXISTS claims_current_key ON claims (
    owner, subject_id, verb,
    (CASE WHEN object_id IS NOT NULL THEN 'e:' || object_id::text ELSE 'l:' || object_canon END),
    polarity)
  WHERE status IN ('grounded', 'unconfirmed') AND valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_claims_timeline ON claims (owner, subject_id, verb);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_subject_entity_fk') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_subject_entity_fk
            FOREIGN KEY (owner, subject_id) REFERENCES entity (owner, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_object_entity_fk') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_object_entity_fk
            FOREIGN KEY (owner, object_id) REFERENCES entity (owner, id);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION claims_valid_mirror() RETURNS trigger AS $$
BEGIN
    IF NEW.status IS NULL THEN
        RETURN NEW;
    END IF;
    NEW.valid := CASE
        WHEN NEW.status = 'grounded' AND NEW.valid_to IS NULL THEN 'live'
        WHEN NEW.status IN ('grounded', 'retracted') THEN 'retracted'
        ELSE 'unknown' END;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_claims_valid_mirror ON claims;
CREATE TRIGGER trg_claims_valid_mirror BEFORE INSERT OR UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_valid_mirror();

CREATE TABLE IF NOT EXISTS claim_evidence (
    owner TEXT NOT NULL,
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL,
    span_id BIGINT NOT NULL,
    speaker TEXT NOT NULL CHECK (speaker IN ('user', 'assistant')),
    act TEXT NOT NULL CHECK (act IN ('assert', 'retract')),
    polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    subject_surface TEXT NOT NULL,
    object_surface TEXT NOT NULL,
    subject_id BIGINT NOT NULL,
    verb TEXT NOT NULL REFERENCES verb (verb),
    object_key TEXT NOT NULL,
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    event_time TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'withdrawn', 'superseded_version')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (owner, claim_id) REFERENCES claims (owner, claim_id),
    FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
    FOREIGN KEY (owner, subject_id) REFERENCES entity (owner, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_evidence_event ON claim_evidence
    (owner, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act)
    WHERE state = 'live';
CREATE INDEX IF NOT EXISTS idx_claim_evidence_timeline
    ON claim_evidence (owner, subject_id, verb, object_key) WHERE state = 'live';
CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence (owner, claim_id);

CREATE TABLE IF NOT EXISTS data_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail JSONB NOT NULL DEFAULT '{}'
);
