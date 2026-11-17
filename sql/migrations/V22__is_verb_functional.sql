-- V22__is_verb_functional.sql — "is" is a single-value attribute; last object wins.
-- Idempotent: safe to re-run.

SET search_path = public;

UPDATE verb SET functional = true WHERE verb = 'is' AND functional IS DISTINCT FROM true;
