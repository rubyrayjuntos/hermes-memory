# Visual Interface Pane — Plan (post-#24)

**Status:** draft plan for board-driven swarm — trunk workflow (`wip/*` → PR → 3 checks → squash). `main` is post-#24 (`feat(infra): hardened compose`, see `git log` / PR #24 — no pinned SHA).

## Goal
Turn the read-only telemetry dashboard into an interactive **Command & Control Drawer** — graph with vector embeddings, metrics, and CRUD for both L2 (pgvector) and L3 (AGE) without dropping to `psql`/Cypher CLI.

**Scope note (AGENTS.md § Out of scope):** `hermes-memory` is a memory provider; full frontend/UI is out of scope here. This plan retains only the **provider/API surface** (`/api/librarian/*` + `SAVEPOINT`/`MERGE`/`bridge` invariants) in this repo. The drawer chrome itself is a thin demo dashboard (`app/index.html` / `docs/graph/index.html` with CDN `vis-network` + demo-data fallback for `*.github.io`) that may move to the owning interface repo if it grows beyond a demo — Issues A–E are scoped accordingly.

Derived from your breakdown (global nav, dependencies/standards/projects/runs/docs/graph views). No image assets needed — text spec below is canonical.

## Architecture guardrails (reuse existing)
- Stack: `apache/age:release_PG17_1.6.0` + `pgvector` on `127.0.0.1:5450` (compose `pgdata`, `hermes_network`), `HYBRID_AGE_DSN` / `HYBRID_AGE_GRAPH=hermes_knowledge`.
- Writes: `SAVEPOINT` / `MERGE` not `CREATE`, `psycopg.sql.Identifier` for identifiers, `check_label`/`validate_graph_name` for graph names, bridge `memory_chunk_nodes (chunk_id, source, vertex_id)`.
- JS/API: AGE IDs are emitted as **decimal strings** end-to-end (`Store.bridge_vertex_ids()` already does, `GET /api/librarian/graph` must too) and kept as strings in `vis-network DataSet(String(id))` / `DataSet(String(from))` — never emit JSON numbers (64-bit `10133099161583617` loses precision in JS). `vis-network` UMD via CDN, demo-data fallback for `*.github.io` (cannot call `localhost:7890` — `CONFIG.API_BASE=''` → fallback).
- Trunk: `main` only durable, `wip/*` scratch deleted same day, `fetch.prune true` / `pull.rebase true`, squash = 1 commit per PR, `required_conversation_resolution` enforced.

## Pane layout (one drawer, three tabs)
```
+-----------------------------------------------------------------------------------+
| 🗂️ LIBRARIAN COMMAND WORKSPACE                      [Close x] [Mode: Split View]   |
|  [ ✍️ Data Editor ]   [ 🔍 Query Console ]   [ 📊 Metrics & Telemetry Deep-Dive ]  |
+-----------------------------------------------------------------------------------+
```

### 1) ✍️ Data Editor — `feat(graph+vector-editor)`
- Inline patch `doc_type` / predeclared edge labels (`GovernedBy`, `Imports` — see `sql/init/01_extensions.sql` `create_elabel`, not `GOVERNED_BY`/`IMPORTS`)/ reassign project scope, `MERGE`/`DELETE` with consequence preview (orphan count, bridge rows affected). Label provisioning is via the existing `create_elabel` + `IF NOT EXISTS` path, no ad-hoc labels.
- Vector sync override: mark stale via separate `embedding_stale` / `embedding_version` metadata (not `metadata.hash` — `hash` is the file content digest, `librarian_health()` counts `hash IS NULL` as drift), re-embed via `nomic-embed-text`, bump stale flag off on success.

### 2) 🔍 Hybrid Search & Cypher Studio — `feat(console)`
- Unified bar: NL hybrid search (`vector <=> $1` + `ILIKE` + `JOIN memory_chunk_nodes` + RRF) alongside raw `SELECT * FROM cypher('hermes_knowledge', $$ … $$)` playground.
- Execution plan: HNSW scan ms / AGE traversal ms / payload assembly ms.
- Dry-run prompt-context preview (`max_tokens`, `vector_k`).

### 3) 📊 Real-Time Observability — `feat(metrics)`
- Drift cards: orphan chunks, `hash IS NULL` where `metadata->>'file_path' IS NOT NULL`, bridge unsynced, `n_dead_tup` / `autovacuum`.
- Latency profiler: ingest run steps, parser/embedding bottlenecks.
- Live audit tail: watchdog events, FS changes (`SSE /api/librarian/events`).

## Issue breakdown (board order)

| # | Issue title | Files | Depends |
|---|-------------|-------|---------|
| **A** | `feat(pane): shell + drawer` | `docs/plans/visual-pane.md` (this), `app/index.html` + `docs/graph/index.html` scaffold, `app/config.js` (`API_BASE`, `FALLBACK_TO_DEMO`), header metrics, project selector, search | — |
| **B** | `feat(graph): topology view` | `src/hermes_memory/api.py GET /api/librarian/graph`, `app/graph.js` (`vis-network DataSet(String(id))`, label filter, 1-2 hop expand) | A |
| **C** | `feat(vector): embeddings inspector` | `GET /api/librarian/chunks/:id/vector`, sparkline/histogram, cosine preview, re-embed trigger | A, B |
| **D** | `feat(console): hybrid search + Cypher studio` | `POST /api/librarian/query`, plan visualizer, context simulator, `POST/PATCH/DELETE /api/librarian/memories` (`SAVEPOINT` Cypher) | B, C |
| **E** | `feat(metrics): observability` | `GET /api/librarian/health` (`store.librarian_health()` scoped), `GET /api/librarian/events` SSE, drift gauge, profiler | A |

Dependency graph:
```
A → B ─┬─→ C ─→ D
A ─────┴─→ E (E parallels B/C — both depend only on A per table, diagram now matches)
```

## API sketch (new, additive)
```
GET  /api/librarian/health
GET  /api/librarian/graph?graph=hermes_knowledge&label=File&limit=100
GET  /api/librarian/chunks?file_path=src/hermes_memory/ingest.py
GET  /api/librarian/chunks/:id/vector
POST /api/librarian/query { q, cypher? }
POST /api/librarian/memories {content, file_path, doc_type}
```

## GitHub Board swarm — approach
- Use **local `board.md` as primary** (proven in v0.1 + C1–C6, avoids merge race). Mirror to **GitHub Project** for visibility via nightly `gh project sync` (PAT needs `project:write`).
- Columns `Todo → In Progress → Review → Done`; one active `wip/*` at a time; claim via `assignee` + `Status=In Progress` to prevent double-pick.
- Poller alternative (board-native swarm) is second iteration after A lands: `cron` every 10m `gh project item-list` or `issues` webhook. `GITHUB_TOKEN` alone insufficient for cross-repo project writes — needs fine-grained PAT.
- Keep `hermes-memory-postgres:local` build, `pgdata` volume name (data-loss guard from #24), `127.0.0.1:5450` default / `6432:5432` pooled.

## Exit criteria
- Pane renders on `localhost:7890` (Flask same-origin, CORS off) and on `*.github.io` with demo fallback.
- Graph ↔ vector round-trip: select node → vector panel → cosine → edit → re-embed → health drops to 0.
- Query console returns hybrid fusion with plan timings; CRUD mutates AGE + bridge + pgvector consistently.
- Metrics show drift/health live; no trunk hygiene regression (squash, prune, delete).

## Next PR
`wip/docs-visual-pane-plan` → merges this doc. Follow-ups: create Issues A–E + Project, then `wip/pane-shell` (#A).
