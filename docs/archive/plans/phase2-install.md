# Phase 2 — Production Install Plan: hybrid-age → live Hermes

**Status:** DRAFT for approval · **Date:** 2026-08-25
**Goal:** Make Ray the first real user — hybrid-age becomes his daily memory provider.

---

## 0. Decisions locked

| Decision | Choice | Rationale |
|---|---|---|
| Database | Main `hermes-postgres` (host port 5440, db `hermes`) | Continuity: pipeline scripts + crons already point here; schema is extractor-ready |
| Embeddings primary | **OpenAI `text-embedding-3-small` @ 768 dims** via existing API key | Removes always-on Ollama dependency; negligible cost at personal scale; native dim truncation to 768 keeps schema unchanged |
| Embeddings fallback | Local Ollama `nomic-embed-text` (768d) | Provider's fallback ladder already supports it; offline mode preserved |
| Dimensions | **Stay at 768** (locked) | Native size for both providers; adequate for conversational recall; zero schema migration. Upgrade path documented in README |
| Specialists | None needed | Contract research complete (Phase A brief); risks are operational |

## 1. Pre-flight fixes (blockers)

- [ ] P1. `~/.hermes/.env`: replace invalid line `HYBRID_AGE_DSN = 'RSwanAN123'`
      with full DSN form:
      `HYBRID_AGE_DSN=postgres://hermes:<password>@localhost:5440/hermes`
- [ ] P2. Confirm OPENAI_API_KEY present and valid for embeddings
- [ ] P3. Ollama reachable check (`http://localhost:11434/v1`) for fallback config

## 2. Code change (small, in-repo)

- [ ] C1. `src/hermes_memory/config.py`: add embed provider selection —
      `embed_provider: openai|ollama` (default `openai`), keep
      `embed_url_env`/`embed_model`; OpenAI path uses `OPENAI_API_KEY`.
- [ ] C2. `embed.py`: route through provider choice; both paths enforce the
      768-dim invariant (return None on mismatch).
- [ ] C3. README "Upgrading embedding models" note (see §5 wording).
- [ ] Commit + property suite green + verify.py PASS against a scratch compose
      stack before touching production config.

## 3. Install sequence

- [ ] I1. Plugin install (directory method, per Hermes brief):
      `mkdir -p ~/.hermes/plugins/hybrid-age && cp -r src/hermes_memory/* ~/.hermes/plugins/hybrid-age/`
      — confirm `__init__.py` contains discovery markers.
- [ ] I2. Config: set `memory.provider: hybrid-age` + `hybrid_age:` block in
      `~/.hermes/config.yaml` (keys per plan §3.2); secrets remain in `.env`.
- [ ] I3. Exercise `hermes memory setup hybrid-age` wizard as validation of the
      setup path we shipped.
- [ ] I4. Backfill/re-embed: DB currently near-empty post-wipe — run ingest on
      active docs if desired, or start empty (conversations accrue naturally).

## 4. Verification gate (all must pass before relying on it)

- [ ] V1. `python -m hermes_memory.verify --dsn <prod DSN>` → PASS all layers
- [ ] V2. Real conversation turn in a test session → rows appear in
      `conversations` AND `memory_entries` within one drain cycle
- [ ] V3. Prefetch appears in prompt context (check recall_status / ask agent
      something only answerable from stored memory)
- [ ] V4. Kill Ollama → confirm no breakage (OpenAI path unaffected)
- [ ] V5. Independent read-only verifier agent confirms discovery + auth +
      write path end-to-end

## 5. README note (embedding upgrade path)

Add under Configuration:

> **Note — upgrading embedding models/dimensions:** vectors are dimension-
> locked to the model that created them. To switch (e.g. nomic-embed-text →
> text-embedding-3-small, or 768 → 1536): (1) stop writes, (2) re-embed all
> rows (`memory_entries`, `doc_chunks`, `conversations`) with the new model,
> (3) if dims change, `ALTER TABLE ... ALTER COLUMN embedding TYPE vector(N)`
> first, then rebuild HNSW indexes after bulk load. At small scale (<50k rows)
> this is minutes. The bridge table needs no changes — vertex ids are
> dimension-independent.

## 6. Rollback

Set `memory.provider: ''` in config.yaml → instant return to built-in SQLite
provider. No data loss; Postgres rows persist for retry.

## 7. After verification passes

Re-enable paused crons one at a time (graph-extractor → doc-librarian-index →
librarian-watchdog), watching last_status between each.
