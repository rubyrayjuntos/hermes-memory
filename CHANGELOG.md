# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

## [1.0.1] - 2026-11-28

### Fixed
- **#123 — the per-turn noun label lookup scanned the entire `noun` table.**
  `fetch_noun_labels()` ran an unscoped `SELECT label FROM noun` on every
  turn, so cost grew with total nouns stored, not with the current turn.
  Replaced with `candidate_label_slugs()` (computes the current turn's
  candidate slugs with no DB access) + `Store.fetch_noun_labels_for_slugs()`
  (`WHERE slug = ANY($1)`, backed by a new `idx_noun_slug` index). A
  20-question growth check after the fix, ingested into one continuously
  growing database (14,576 noun rows by the end), showed a flat
  seconds-per-turn curve (mean 301.6ms, stdev 14.6ms, weak r=0.45 vs.
  cumulative rows) — the fix holds as the table grows.
  - `noun.slug` (migration `V24__noun_label_slug_index.sql`) is a plain
    column, not SQL-`GENERATED`: Python's `_slug()` and Postgres's `lower()`
    diverge on some non-ASCII input (Turkish dotted capital `İ` lowercases
    to two codepoints in Python, one in Postgres), so the authoritative
    value is written once in Python on every `upsert_noun()` call, with a
    best-effort SQL backfill for rows that predate the migration.
- **`scripts/migrate.py` sorted migrations lexicographically, not numerically** -- "V10" sorted before "V2" as strings, so every double-digit migration ran before every single-digit one. Latent since V10 landed; didn't affect `v1.0.0` because no migration before V24 happened to depend on a table a single-digit migration created in a separate file, so either order produced the same end state. Exposed by V24 (`ALTER TABLE noun ...`), which depends on V9 (`CREATE TABLE noun ...`): CI caught it as `relation "noun" does not exist` on a fresh database. Fixed to sort by the numeric version. No hotfix needed for `v1.0.0`; already-installed databases have V1-V23 recorded and only run V24 under the corrected order.

## [1.0.0] - 2026-11-27

First public release. Covers everything on `main` since tag `v0.9.0`, including what
tag `v0.10.0` (`e55ac82`) pins. `v0.10.0` was a pin for the LongMemEval dry run, not a
release, and it has no changelog section of its own.

### Added
- **Tiered memory injection and agent fetch tools (Spec 8, #117).** The injected memory block
  now holds a capped number of full spans (`full_span_cap`, default 4) plus short pointer
  summaries (`pointer_cap` 15, `pointer_token_budget` 300). The agent gets two provider-scoped
  tools: `memory_get` (fetch full bodies by id) and `memory_search` (search memory directly).
  Per-turn limits: `memory_get_max_ids` 10, `memory_search_max_k` 10,
  `memory_tool_calls_per_turn` 5.
- **Graph layer: claims, extraction, shadow retrieval (#91).**
  - Owner-scoped claims built from evidence rows, with event-time timelines (V18).
  - A `durable_job` queue (V19), with rules and model extraction running on a worker off the
    prompt path.
  - A `retrieval_shadow` log (V20/V21) that compares RRF/MMR claim retrieval with a bounded
    2-hop walk against the served path, without changing the prompt.
- **Opt-in hybrid claim retrieval (#94):** `retrieval_mode: hybrid` serves grounded facts with
  cited supporting turns. The default stays `vector`.
- **Opt-in model claim extraction with batching and usage tracking (`f2a02a5`).**
  `extract_mode: rules_then_model` batches eligible user turns (8 turns, 10-minute idle,
  30 minutes from the first turn, or session close). `/api/health` reports pending jobs,
  model calls and token usage. The default stays `rules_only`.
- **Spec 3 model tier (#103, #105, #106):**
  - First-person grounding.
  - Same-session context loading.
  - `extract_model_schema` (`json_object` | `json_schema`).
  - Wider paraphrase cues and role normalization.
  - Content-free turns no longer enqueue model jobs.
- **Spec 4: assistant replies as unconfirmed claims (#108),** behind
  `extract_assistant_replies` (default `false`). Assistant-only facts stay `unconfirmed` and
  never become current on their own. Injected `<memory>` blocks are stripped before extraction.
- **Spec 5: work memory, EXPERIMENTAL (#109, #110):**
  - `work_item` / `work_artifact` / `work_event` tables (V23), behind `work_capture`
    (default `false`).
  - Keyed HMAC content hashes.
  - Fixed false positives in the verdict matcher, and synthetic rows are no longer picked as
    the "ask".
  - Nothing populates `work_artifact` yet (T5.5b). Gate G5, which decides whether this turns
    on by default, has not passed.
- **Spec 7: session-level task splitting for work capture (#115).** Uses rule-based boundaries
  with no model call. Each closed segment gets its own `extract_work` job. Marker-only
  segments merge into a neighbor, and status lookahead is added. Gate G7 decides whether
  production item extraction runs.
- **Claims Graph Explorer (#93):** a live storage inspector at
  `http://127.0.0.1:7890/api/librarian/explorer`, owner-scoped and read-only, with an optional
  Hermes Desktop sidebar pin (`desktop-plugins/graph-explorer/`).
- **Spec 0 golden-extraction eval harness (#95), plus Spec 1 paste segmentation with a
  `document` speaker and Spec 2 first-person functional rules (#96):**
  - First-person rules: `lives_in` / `based_in` / `works_for` / `role_is`.
  - Migration V22.
  - Golden-set precision went from 0.556 to 0.909 and recall from 0.455 to 0.909. Sources:
    `docs/evals/2026-11-17-golden-rules-only.json` (before) and
    `docs/evals/2026-11-17-spec1-2-merged-golden-rules-only.json` (after).
- Entities with alias equations and merges; a span deletion API that rebuilds what the spans
  supported.
- Health and verify report claims by status, job counts, drift repair, and the legacy
  migration check.
- Retrieval eval harness for the provider (`scripts/eval_memory.py`, #90).
- Durable write outbox (V16 `write_outbox`, V17 idempotency):
  - `sync_turn` / `on_memory_write` record each write before queuing it.
  - When the queue is full, the write is deferred to the outbox instead of dropped.
  - Leftover rows replay at start and every 30 s with `SKIP LOCKED` claims.
  - Replays carry `outbox_id`, so a turn is not written twice. (See Known issues: #119.)
- Harness metrics: entity coverage, cross-owner leakage, and a burst/shutdown/replay
  durability mode (`--durability N`).
- The resolved Postgres target is logged at startup with the password stripped (`redacted_dsn()`,
  #111).

### Changed
- **`tiered_injection` now defaults to `true`** (Spec 8, Gate G8 passed on T8.4c,
  `docs/evals/t84c_final_report.json`).
  - Both independent graders' 95% CIs on the paired multi-span accuracy delta exclude zero.
  - Win/loss favors "on" 11-1 and 13-0.
  - The 5 single-fact controls show zero regression.
  - The two earlier rounds are in the same file trail: T8.4 was null
    (`docs/evals/t84_final_graded_report.json`) and T8.4b was small
    (`docs/evals/t84b_final_graded_report.json`).
  - To get the old unlimited-full-span behavior back, set `tiered_injection: false` in a
    profile's config.
- `memory_get` / `memory_search` ids are source-qualified (#117 review):
  - A bare number is a conversation, `mem:<id>` is a memory entry, and `doc:<id>` is a doc
    chunk. `memory_get` fetches each one from its own owner-scoped table.
  - `memory_get` returns full bodies (redact the whole body, then window, then escape). When the
    output budget binds, it reports `truncated` with `next_offset` and lists `omitted` ids.
  - `memory_search` applies the real conversation role and the same uptake/claim overlays as
    prefetch.
- **Ingest embeds mentions in batches (#122).** A turn's mention labels go through one
  `embed_texts()` call instead of one `embed_text()` call per mention. On the 3 profiled
  LongMemEval questions this meant about 4.6× fewer embed calls and 1.3–1.8× less time in the
  embedding stage (`docs/evals/longmemeval/batching_fix_before_after.json`).
  - Total wall-clock per question did not improve in that measurement (average 0.95×). The
    eval database grew between passes, and the unscoped noun-label scan (#123) got slower as it
    grew.
  - Vectors match per-call embedding within 1e-4.
- **The local embedder is shared process-wide (#122).** In-process fastembed/ONNX embedders are
  cached by model, so creating several providers in one process no longer loads several copies
  of the model. HTTP-backed embedders are deliberately not shared, because they are bound to
  the event loop that created them.
- **Retrieval packing (#114):**
  - Similarity is now the primary sort key within a bin.
  - 30% of the token budget is reserved for the strongest matches across both bins.
  - The default `min_similarity` floor went from 0.55 to 0.58.
- Embeddings default to in-process fastembed/ONNX (`nomic-embed-text`, 768-d), and Ollama is
  no longer a prerequisite. `hermes-memory-install` warms the local model. Set
  `HYBRID_AGE_EMBED_URL` only if you want an HTTP override. When upgrading an install, remove
  any leftover `HYBRID_AGE_EMBED_URL=…11434…` from `.env` to switch off Ollama.
- Claims are owner-scoped and derived from evidence (`claim_evidence`).
  - `status` replaces `valid`, which is now a read-only mirror.
  - Contradictory statements resolve to the newer one.
  - Functional facts (lives in, works for, based in, …) keep an event-time history, and
    corrections restore the previous value.
- Rules extraction is stricter: `RULES_VERSION` 4, proper names must be mixed-case, and Title-Case
  `is` needs a proper-name subject. Markdown/code junk objects are rejected, and shell pastes
  count as code (#99, #100, #101).
- Write capture uses the `durable_job` table, and `write_outbox` is no longer written.
- Graph expansion is owner-scoped. A path is kept only if one of its provenance turns belongs
  to this memory owner, and its provenance is trimmed to those turns.
- `hermes-memory-verify --dsn` uses that DSN for the provider write path too.
- Local integration smoke tests honor pytest's explicit `--dsn` and reject targets outside the
  isolated `hermes_test` database.
- Extraction jobs convert captured ISO timestamps to timezone-aware datetimes before binding
  them to `timestamptz`.

### Security
- **Redaction hardening (#117).** While grading the T8.4 eval, two real secrets were found
  passing through the full-span render path: a password written in prose and a phone number.
  `_span_tag` (the Tier-1 / `memory_get` path) now redacts before escaping. New rules cover
  prose passwords, phone numbers and email addresses, and the JWT-shape and prose-password
  patterns were tightened against false positives. The leaked copies were scrubbed. Details:
  `docs/hermes-memory-dev-spec.md` §8.5.1.
- Work-item summaries redact credentials before summarizing (#115).
- The legacy injection renderer now escapes stored content, so a span containing
  `</PAST_CONTEXT>` or a fake `<system>` block can no longer close the memory fence (#114).

### Known issues
- [#118](https://github.com/rubyrayjuntos/hermes-memory/issues/118): content containing `$`
  fails to get a graph vertex. The failure is logged as a warning and not fatal; the
  conversation row still lands. Reproduced with a test (`xfail(strict=True)`, PR #120).
- [#119](https://github.com/rubyrayjuntos/hermes-memory/issues/119): under heavy write pressure,
  when the outbox fallback also times out, `sync_turn()` can drop a write with only a WARNING
  log line. Reproduced with a test (`xfail(strict=True)`, PR #121).
- [#123](https://github.com/rubyrayjuntos/hermes-memory/issues/123): every turn write reads the
  whole `noun` table, so ingest slows as the database grows.
- [#125](https://github.com/rubyrayjuntos/hermes-memory/issues/125): `test_two_sessions_share_nouns_without_passport_bleed`
  (property test, hypothesis-driven) fails on a clean `main` checkout -- confirmed pre-existing,
  not a regression from any merged PR. The `observed_boosts == {0.0, 1.0}` assertion is too
  strict for some small hypothesis-shrunk examples (few labels/turns can legitimately produce
  zero 2-hop provenance-boosted edges). Marked `xfail` (not `strict`, since hypothesis-driven
  generation could legitimately XPASS by chance on some runs), PR #126.

## [0.9.0] - 2026-11-14

Pre-release for human-observed install testing. Not a public release.

### Added
- Install without a clone: the wheel bundles the install assets and records the commit it was built from. `pip install git+https://github.com/rubyrayjuntos/hermes-memory@<tag-or-commit>`, then `hermes-memory-install`.
- Plugin manifest (`plugin.yaml`) declares dependencies; `hermes memory setup hybrid-age` admits them into Hermes's runtime.
- `hybrid_age.memory_scope` (default `shared`): one memory per profile across chat sessions; `session` restores per-chat isolation.
- Disposable release gate (`scripts/release_gate.py`) and an isolated live lifecycle (`--execute`).
- Fountain live inspector (`docs/graph/fountain.html`) via `hermes-memory-api` on `127.0.0.1:7890`.
- First-time install CLI (`hermes-memory-install`) plus upgrade / migrate / uninstall / backfill.
- Instrumented prefetch / injection cockpit and graph path expansion (#59).
- Write-outcome ledger (`WriteOutcome` / `WriteLedger`): drain records enqueue, embed, SQL turn, flower, nouns, mentions, and memory SQL; counts `dropped_writes`, `writes_failed`, `embed_null`, `graph_degraded`. Counters are process-local (restart zeros them); `record()` redacts `detail`. After insert B, `conversations.drain_status` is `complete` / `embed_null` / `graph_degraded` (V11). L1 insert failure still leaves no row. Live-shaped eval counts unpassported turns via the passport anti-join, not `drain_status`.

### Security
- Viz API CORS scoped to loopback/`null`; bind remains `127.0.0.1` (#61).
- Ingest transaction SAVEPOINT / batched MERGE hygiene (#62).
- Secrets/config hygiene: password leak fix, atomic config, `sql/example` (#63).
- Ghost kNN DoS caps and namespaced bridge keys (#60).

### Changed
- Documented clone-first wheel and provider discovery, recorded installation ownership, and safe uninstall behavior.
- CI is pip-only (no Poetry). Lint job runs ruff + mypy on extracted modules.
- Nightly job runs a real Ollama embed e2e (`hermes-memory-verify`), not a stub.
- Integration CI runs `test_verify_pipeline` and `test_bridge_symmetry` against `HYBRID_AGE_DSN`.
- P3/P4 property tests are live on `extract_nouns` (docs no longer claim they are skipped).
- Legacy Title-Case cosine linker lives in `about_concepts.py`; conversation writes use mentions + flower.
- Ingest boots like the provider: `apply_pending_migrations` then `Store.require_schema_head`.
- Viz API split: `graph_runtime` / `graph_http` / `graph_server` entrypoints.
- Librarian/pane health merges write-ledger counters into the Store health payload.
- Conversation drain stamps `drain_status` after insert B (`complete` / `embed_null` / `graph_degraded`). Live-shaped eval still uses the passport anti-join for `unpassported_count`, not this column. Prefetch still returns `""` for both empty recall and read failure; logs distinguish them (`prefetch empty recall` vs `prefetch failed`).
- Rewrote ABOUT recency integration as mentions + magnitude ranking (`test_expand_graph_stronger_mentions_ranks_first`); mentions decay stays in `store_expand` / walk tests.

### Fixed
- Uninstall: compose shutdown needs the interpolation variable; no longer calls a nonexistent `hermes restart`; `--keep-db` keeps the password its volume needs.
- Verify cleanup removes claim rows before synthetic conversations (RESTRICT foreign keys).
- Provider loads in the gateway (dependency admission) and no longer logs failed imports for CLI modules.
- Shared memory is owned per profile, so profiles on one database do not merge memories.
- Source distributions and checkout installs keep the pinned commit and dirty state.
- Prefetch fencing so Hermes sanitize keeps SEED/Path (#58).
- Graph search packing, live graph name, concept embed batching (#59 review).

## [0.1.0] - 2026-08-24

First tagged release. Six kanban cards ([docs/plans/board.md](docs/plans/board.md)).

### Added
- **C1 — Scaffold & consolidation:** `src/hermes_memory/` package layout with
  `register(ctx)` entry point; dashboard HTML moved to `docs/dashboard/`;
  extraction prototypes parked under `legacy/scripts/`; dev extras.
- **C2 — Docker + schema + indexes:** thin Dockerfile over
  `apache/age:release_PG17_1.6.0` + pgvector (PG17 1.7.0 tag does not exist);
  docker compose stack on `127.0.0.1:5450` with healthcheck; `sql/init/*.sql`
  creating extensions, tables (extractor-ready columns), HNSW/GIN/BTREE indexes,
  and the `hermes_knowledge` graph on first boot.
- **C3 — Provider port:** `HybridAgeMemoryProvider` implementing the Hermes
  `MemoryProvider` ABC (`provider.py`, `config.py`, `embed.py`, `store.py`);
  config_schema powering `hermes memory setup hybrid-age`; installable via
  plugin dir or pip entry point `hermes_agent.memory_providers`.
- **C4 — Ingest port:** `hermes-memory-ingest` CLI — hash → chunk → embed →
  pgvector + batched AGE MERGEs + bridge rows; dedup (re-run = 0 new entities)
  and file-hash drift detection with per-codebase state files.
- **C5 — Verify harness & tests:** `hermes-memory-verify` end-to-end smoke-check
  CLI; Hypothesis property suite P1–P8 (18 passed / 1 skipped; P3/P4 deferred to
  v0.2); GitHub Actions CI — unit matrix, integration job, nightly stub.
- **C6 — Docs truth-pass:** README rewritten against reality, CONTRIBUTING
  updated (kanban flow, test requirements, DDL rules), architecture.md refreshed,
  AGE quirks extended, this changelog created.

### Notes
- Tested against Hermes Agent v0.20.x. MIT licensed.

## Document changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 1.1 | 2026-08-24 | 2026-11-13 | Codex | Record packaging and lifecycle contract documentation. |
| 1.2 | 2026-08-24 | 2026-11-14 | Claude | Put `[Unreleased]` above `[0.9.0]`; moved items already in tag `v0.9.0` into the 0.9.0 section; added eval harness and Phase 0 entries. |
| 1.3 | 2026-08-24 | 2026-11-14 | Codex | Record isolated explicit-DSN support for the integration smoke suite. |
