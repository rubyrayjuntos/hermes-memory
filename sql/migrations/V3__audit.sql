-- V3__audit.sql — audit tables for agent decisions and schema changes
-- Idempotent via IF NOT EXISTS. Append-only; no FK to allow audit of dropped objects.

SET search_path = public;

-- Agent decision trace (why the agent chose what it did)
CREATE TABLE IF NOT EXISTS agent_decision_audit (
    id BIGSERIAL PRIMARY KEY,
    agent_identity TEXT NOT NULL DEFAULT 'default',
    session_id TEXT,
    decision TEXT NOT NULL,
    rationale TEXT,
    context JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_decision_audit_session
    ON agent_decision_audit (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_decision_audit_agent_time
    ON agent_decision_audit (agent_identity, created_at);

-- Schema change audit (mirrors migration_history but human-readable for auditors)
CREATE TABLE IF NOT EXISTS schema_change_audit (
    id BIGSERIAL PRIMARY KEY,
    version TEXT NOT NULL,
    filename TEXT NOT NULL,
    checksum TEXT,
    description TEXT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    execution_time_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_schema_change_audit_version
    ON schema_change_audit (version);
CREATE INDEX IF NOT EXISTS idx_schema_change_audit_applied
    ON schema_change_audit (applied_at);
