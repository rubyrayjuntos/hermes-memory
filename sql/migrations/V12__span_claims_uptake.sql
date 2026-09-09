-- V12__span_claims_uptake.sql — spans + claims + uptake tables.
-- Idempotent. New tables only; no changes to existing tables or code paths.
-- Reads of conversations/noun/semantic_edge are unaffected.
--
-- Contract (see_HANDOFF/design notes):
--   spans are NOT a new table: conversations rows already are spans
--   (one speaker, one clock, one string). uptakes/claims/aliases reference them.
--   uptake rows exist iff the pair is assistant->user. claims start empty:
--   missing structure is allowed, fake structure is not.
--   aliases come from explicit equations or deterministic normalization only,
--   never from embedding similarity.

SET search_path = public;

CREATE TABLE IF NOT EXISTS aliases (
    alias_id BIGSERIAL PRIMARY KEY,
    surface_norm TEXT NOT NULL,
    canon_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user_span', 'doc', 'manual')),
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    valid TEXT NOT NULL DEFAULT 'live' CHECK (valid IN ('live', 'retracted')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One live mapping per surface: retraction writes a new row, old row flips
-- to retracted. No UNIQUE on the column (history is kept); uniqueness of the
-- live mapping is enforced by the partial index below.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_live_unique
    ON aliases (surface_norm) WHERE valid = 'live';
CREATE INDEX IF NOT EXISTS idx_alias_canon ON aliases (canon_id) WHERE valid = 'live';

CREATE TABLE IF NOT EXISTS uptakes (
    uptake_id BIGSERIAL PRIMARY KEY,
    prior_span_id BIGINT NOT NULL REFERENCES conversations (id) ON DELETE RESTRICT,
    next_span_id BIGINT NOT NULL REFERENCES conversations (id) ON DELETE RESTRICT,
    value TEXT NOT NULL CHECK (value IN
        ('accepted', 'used', 'repaired', 'abandoned', 'unknown')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_uptake_pair UNIQUE (prior_span_id, next_span_id)
);
-- No pending_next here: rows are written only when the next span exists.
-- Values are reception verdicts only, never placeholders.

CREATE TABLE IF NOT EXISTS claims (
    claim_id BIGSERIAL PRIMARY KEY,
    span_id BIGINT NOT NULL REFERENCES conversations (id) ON DELETE RESTRICT,
    subject TEXT NOT NULL,
    verb TEXT NOT NULL,
    object TEXT NOT NULL,
    polarity TEXT NOT NULL DEFAULT 'positive'
        CHECK (polarity IN ('positive', 'negative')),
    act TEXT NOT NULL CHECK (act IN ('ask', 'assert', 'retract', 'plan', 'quote', 'tool')),
    valid TEXT NOT NULL DEFAULT 'live' CHECK (valid IN ('live', 'retracted', 'unknown')),
    superseded_by BIGINT REFERENCES claims (claim_id) ON DELETE RESTRICT,
    uptake TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_claims_live_subject
    ON claims (subject, verb) WHERE valid = 'live';
CREATE INDEX IF NOT EXISTS idx_claims_span ON claims (span_id);
