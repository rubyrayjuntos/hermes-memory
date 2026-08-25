# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

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
