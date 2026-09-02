<img width="2048" height="1152" alt="hermes_librarian_banner" src="https://github.com/user-attachments/assets/109ea41f-2787-443f-a815-18139f80da4c" />
# 📜 The Hermes Librarian 📜

**Persistent memory & context injection for [Hermes Agent](https://github.com/nousresearch/hermes-agent)** —
pgvector for semantic recall + Apache AGE knowledge graph for entity expansion,
in one Postgres database.

> *A librarian doesn't just store books — she knows where everything is, retrieves
> the right volume at the right moment, and quietly discards what's stale.*

Package: `hermes-memory` · Product: **The Hermes Librarian**

- **Provider name:** `hybrid-age` (plugin dir or pip entry point)
- **Stack:** `apache/age:release_PG17_1.6.0` + pgvector, exposed on `127.0.0.1:5450`
- **Embeddings:** Ollama (`nomic-embed-text`, 768-dim) via OpenAI-compatible API
- **Tested against:** Hermes Agent v0.20.x
- **License:** MIT

## Quick Start

Follow the steps in order — each depends on the previous one.

```bash
# 1. Clone
git 
clone https://github.com/rubyrayjuntos/hermes-memory.git
cd hermes-memory

# 2. Configure environment — docker compose REQUIRES HERMES_PG_PASSWORD
cp .env.example .env
# Edit .env and set HERMES_PG_PASSWORD (and optionally HYBRID_AGE_DSN)

# 3. Start Postgres 17 + Apache AGE 1.6 + pgvector on 127.0.0.1:5450
docker compose up -d
docker compose ps        # wait until "healthy"
# Need connection pooling? `docker compose --profile pooled up -d` also starts PgBouncer on 127.0.0.1:6432
# sql/init/*.sql create extensions, the hermes_knowledge graph, all labels,
# tables and indexes automatically on first boot.
# Data persists in the pgdata volume across docker compose down/up.

# 4. (optional, but needed for tests / CLIs) editable install with dev extras
pip install -e '.[dev]'

# 5a. Install as a Hermes plugin — copy the package into the plugins dir,
#     keeping __init__.py at the top level (Hermes' discovery heuristic
#     scans that file):
mkdir -p ~/.hermes/plugins/hybrid-age
cp src/hermes_memory/__init__.py src/hermes_memory/*.py \
   ~/.hermes/plugins/hybrid-age/

#    ...OR install via pip — Hermes also discovers providers through the
#    `hermes_agent.memory_providers` entry point declared in pyproject.toml:
pip install -e .

# 5b. Configure ~/.hermes/config.yaml
```

```yaml
# ~/.hermes/config.yaml
memory:
  provider: hybrid-age          # activation key

hybrid_age:                     # optional overrides (defaults shown)
  dsn_env: HYBRID_AGE_DSN       # DSN comes from this env var (secrets stay out of yaml)
  embed_url_env: HYBRID_AGE_EMBED_URL
  embed_model: nomic-embed-text # must produce 768-dim vectors
  graph: hermes_knowledge
  vector_k: 12
  min_similarity: 0.55
  max_tokens: 1200
```

**Alternative to step 5b:** because the package ships a `config_schema`,
`hermes memory setup hybrid-age` walks through the same settings interactively
(secret fields are written to env, behavior knobs to config.yaml).

### Verify it works

With the stack up and the plugin installed:

```bash
hermes-memory-verify            # synthetic turn → per-layer row-count assertions; exit 0 = PASS
```

Or index a codebase and check recall:

```bash
hermes-memory-ingest path/to/repo   # hash → chunk → embed → vector + AGE graph + bridge rows
hermes-memory-ingest path/to/repo   # re-run: dedup means 0 new entities
```

## Architecture

The memory core spans **three coordinated layers**:

| Layer | What it holds | How it's built |
|---|---|---|
| **Conversations** | `conversations` table — one row per turn (user + assistant), contains `session_id`, `turn_id`, `content`, `created_at` ISO | `sync_turn` per-turn: embed Turn → extract 1-3 Concepts → MERGE Turn/Concept/ABOUT → bridge `memory_chunk_nodes` |
| **Vector + Graph** | `memory_entries` (pgvector HNSW, 768-dim nomic-embed-text) + `AGE hermes_knowledge` graph (File/Module/Dependency/Standard/Concept/ Turn nodes + typed edges) | Ingest walks codebase → SHA-256 chunks → embed → File/Module/Dependency with `IMPORTS` edges (weight 1.0 internal / 0.80 external, cosine 0.80). AGE labels via `create_vlabel`/`create_elabel`. |
| **3D Inspector** | Live growth view: `docs/graph/3d.html` polls `/api/librarian/graph/stats` 30s + `/api/librarian/graph/3d` 20s (paused during panel inspect). Edge labels: `IMPORTS w1.0·c0.80` and `ABOUT w1.0·c0.55-1.0` (orange). Filter toggles `IMPORTS`/`ABOUT` hide/show without reload. | `3d.html` JS: `fetchMetrics()` + `load()` + `String(id)` for vis-network IDs. Badge `#verifyBadge` top-right polls `/api/librarian/verify` → `PASS/FAIL · <len> chars · <lines> lines`, fallback to graph/stats placeholder. |

**Walk** (expand_graph): `0.5*cosine + 0.3*weight + 0.2*exp(-age/30)` where age from `r.created_at / m.created_at`, legacy fallback 0.5. Score-order `DESC`, gates `min_cosine=0.0`, `min_weight=0.0`. Radius gate `r.cosine >= $radius` with `ORDER BY (0.5*r.cosine + 0.3*COALESCE(r.weight,0.5) + 0.2*exp(-age/30.0)) DESC`.

**Concept compaction** (cosine≥0.92): Union-Find `compaction_keepers` keeps min-id deterministic; `merge_concept_pair` rewires edges via `SET e += {weight, cosine}` bare numeric via `age_props`; `prune_orphan_concepts` deletes degree==0 + created_at>7d; bridge `UPDATE ... AND graph_name=$1` dedup.

**Verify**: `hermes-memory-verify PASS 4/4 len 4022` (`db-connectivity`, `conversations 2`, `memory_entries mirror 1`, `prefetch len 4022`). `health healthy:true`; `isolated_files 36` expected drift until ABOUT bridges fill; `orphan_chunks 0`.

```text
User message ─▶ sync_turn ──► embed Turn(768d) ──► extract Concepts (regex, stopwords, 45-char max, max 3) ──► cosine dot/√ ──► MERGE Turn {session_id, turn_id:int, content[:200] weight:1 created_at} + Concept {name weight:1 created_at} + ABOUT {weight:1, cosine:real} ──► bridge_turn conv_<id>/conversation ──► memory_chunk_nodes ──► graph growth (w×c inspector)
                                    │
                                    ▼
                     AGE hermes_knowledge (File/Module/Dependency IMPORTS edges + Concept nodes + ABOUT edges from turns)
                                    │
                                    ▼
                     expand_graph(seed, [], limit, min_weight, min_cosine, decay_half_life) → 0.5·cos+0.3·weight+0.2·exp(-age/half_life) DESC
```
|| Embeddings | `src/hermes_memory/embed.py` | OpenAI-compatible client (Ollama), 768-dim invariant |
|| Store | `src/hermes_memory/store.py` | SQL/Cypher data access; every Cypher statement inside a SAVEPOINT |
|| Ingest | `src/hermes_memory/ingest.py` | Codebase indexing: hash→chunk→embed→vector+graph+bridge |
|| Verify | `src/hermes_memory/verify.py` | End-to-end pipeline smoke-check CLI |

|| Layer | Tech | Purpose |
||-------|------|---------|
|| Storage | Postgres 17 + pgvector | Embeddings (HNSW) + conversation/memory/doc-chunk rows |
|| Graph | Apache AGE 1.6 | Entity vertices + typed edges in the `hermes_knowledge` graph |
|| Bridge | `memory_chunk_nodes` | Links vector chunks to AGE vertex IDs |
## Scope: v0.1 vs v0.2

| Capability | v0.1 | v0.2 roadmap |
|------------|:----:|--------------|
| Docker compose stack (AGE PG17 1.6 + pgvector) | ✅ | |
| Memory provider (turn capture, budgeted prefetch injection) | ✅ | |
| `hermes memory setup` support (config_schema) | ✅ | |
| Doc/codebase ingest CLI (`hermes-memory-ingest`) | ✅ | |
| Verify CLI (`hermes-memory-verify`) | ✅ | |
| Property test suite P1–P8 (18 passed / 1 skipped*) | ✅ | |
| CI (unit matrix + integration job + nightly stub) | ✅ | |
| Conversation graph extractor | | ✅ (stubs live in `legacy/scripts/`) |
| Relation extractor / semantic edges | | ✅ |
| Watchdog & orphan sweeps | | ✅ |
| Dashboard live mode (static demo only today) | | ✅ (`docs/dashboard/`) |
| Multi-graph support, embedding-model swap tooling | | ✅ |

\* P3/P4 (phrase-extraction properties) are deferred to v0.2 with the extractor port.

## Testing

```bash
pip install -e '.[dev]'

pytest tests/property -q        # pure-function Hypothesis property tests (no DB)
pytest -q                       # default addopts exclude integration
pytest tests/integration -q -m integration --override-ini="addopts="   # needs the compose stack up
```

What the property suite guards:

| ID | Guards against |
|----|----------------|
| P1 | Unescaped `'`/`\` surviving `age_str` (Cypher injection) |
| P2 | Malformed MERGE props (None dropped, keys preserved once) |
| P3/P4 | Extractor noise / concept self-replication *(v0.2 — skipped)* |
| P5 | Garbage module names from artifact paths (hex/uuid/digits/skip-dirs) |
| P6 | Nondeterministic dependency-index output |
| P7 | Silent file-hash state corruption (corrupt JSON → `{}`) |
| P8 | Orphan bridge rows (dedup key + insert/delete symmetry) |

The verify CLI exercises all three layers end-to-end (synthetic turn → row counts
in conversations, memory_entries, and the AGE graph) and exits non-zero if the
background drain stalls:

```bash
hermes-memory-verify [--dsn postgres://...] [--drain-wait SECONDS]
```

## Configuration reference

### config.yaml — `hybrid_age:` block (behavior knobs)

| Key | Default | Meaning |
|-----|---------|---------|
| `dsn_env` | `HYBRID_AGE_DSN` | Name of the env var holding the Postgres DSN |
| `embed_url_env` | `HYBRID_AGE_EMBED_URL` | Name of the env var holding the embeddings endpoint |
| `embed_model` | `nomic-embed-text` | Embedding model (must be 768-dim) |
| `graph` | `hermes_knowledge` | AGE graph name |
| `vector_k` | `12` | Vector-search top-k |
| `min_similarity` | `0.55` | Minimum cosine similarity for recall candidates |
| `max_tokens` | `1200` | Injection budget (tokens) |

Resolution order: config.yaml > environment > defaults. Secrets/endpoints never go
in committed yaml — they resolve from the env vars named by `dsn_env`/`embed_url_env`.

### Environment variables

| Variable | Required | Meaning |
|----------|----------|---------|
| `HERMES_PG_PASSWORD` | yes (compose) | Postgres password; compose refuses to start without it |
| `HYBRID_AGE_DSN` | yes (provider) | e.g. `postgres://hermes:<pw>@localhost:5450/hermes_memory` |
| `HYBRID_AGE_EMBED_URL` | no | Embeddings endpoint (default `http://localhost:11434/v1`) |
| `HYBRID_AGE_EMBED_MODEL` | no | Overrides `embed_model` when yaml doesn't set it |
| `HYBRID_AGE_GRAPH` | no | Overrides `graph` when yaml doesn't set it |
| `HERMES_MEMORY_DSN` | no | Convenience alias defined in `.env.example` only — prefer `HYBRID_AGE_DSN` (nothing in src reads this) |
| `HERMES_MEMORY_GRAPH` | no | Convenience alias only — prefer `HYBRID_AGE_GRAPH` |

## Troubleshooting

- **"char limit raised but nothing more is stored."** `memory_char_limit` /
  `user_char_limit` cap how much context is *injected per turn* — they do not
  change what is stored or retrieved. The injection budget is governed by
  `max_tokens` (default 1200); raising the char limits alone won't surface more memory.
- **Prefetch silently returns empty after ~8s.** Hermes kills prefetch at 8 seconds.
  Warm prefetch should run well under 2s. If you're hitting the cap, check that
  Ollama is reachable (`HYBRID_AGE_EMBED_URL`) and that the HNSW indexes exist
  (`sql/init/03_indexes.sql`). On any failure the provider returns `""` rather than raising.
- **A failed Cypher statement poisons the whole transaction.** In AGE, an error
  aborts the transaction, not just the statement. The store layer wraps every
  Cypher call in a SAVEPOINT and rolls back to it; keep that pattern if you add SQL
  (see [docs/age-quirks.md](docs/age-quirks.md)).
- **`ON CREATE SET` / `ON MATCH SET` don't exist in AGE 1.6** ([plan §3.3](docs/plans/v0.1.md)):
  use MERGE-then-SET — `MERGE (v:Label {key}) RETURN v;` then
  `SET v += {props}` in a second statement, keeping the merge key out of the SET map.
- **GIN index note.** Containment queries issued as raw SQL against the label's
  underlying table hit the GIN `properties` index (verified with Bitmap Index Scan).
  Cypher-internal `properties @>` predicates may bypass the index — prefer raw SQL
  for hot containment lookups.

## Version compatibility

Tested against **Hermes Agent v0.20.x**. Newer Hermes versions regularly add kwargs
to provider methods; the provider accepts `**kwargs` everywhere, but pin expectations
until re-tested.


## Operations

### CLI commands (new in 0.2)

| Command | What it does |
|---|---|
| `hermes-memory-install` | Full first‑time setup: docker compose, plugin copy (default + librarian profile), pip install, `memory.provider=hybrid-age`, `hybrid_age` block, `hermes-memory-api` on 127.0.0.1:7890, verify. |
| `hermes-memory-upgrade` | From any prior version to 0.1.0: back up schema, run migrations, rewrite DSNs, restart API, verify. |
| `hermes-memory-migrate` | Dump data from a source DSN, apply V6 constraint fix, restore to target DSN, optionally re‑ingest codebase. |
| `hermes-memory-uninstall` | Stop viz API, disable hybrid‑age, drop hermes_memory DB (unless `--keep-db`), remove plugin dirs, fall back to built‑in MEMORY.md/USER.md. |
| `hermes-memory-api` | Read-only viz API (`start`/`stop`/`status`/`serve`) on 127.0.0.1:7890. Pane: `http://127.0.0.1:7890/api/librarian/pane`. |
| `hermes-memory-backfill` | Graph unlinked `conversations` via the provider Turn linker (skips C5 verify synthetics). |

### Backup & restore

```bash
# Backup the hermes_memory database (custom, compressed format)
pg_dump -Fc hermes_memory > ~/librarian-$(date +%F).dump

# Restore
pg_restore -d hermes_memory ~/librarian-2026-08-31.dump
```

### Between‑version migration notes

- **AGE 1.7.0 → 1.6.0**: the provider uses `MERGE‑then‑SET +=` (no `ON CREATE SET`/`ON MATCH SET`).
- **Canonical dedup** (AGENTS.md): `memory_entries` dedup is on `(agent_identity, target, md5(content))` via `memory_entries_unique_hash` UNIQUE index on `md5(content)` — plain `UNIQUE (agent_identity, target, content)` exceeds btree 2704-byte limit. Fresh installs get the correct index from `sql/init/02_schema.sql`. Deployments that already applied `V6__fix_memory_entries_conflict_target.sql` (plain content UNIQUE) should apply `V7__canonical_md5_dedup.sql` which drops the plain constraint and creates the `md5(content)` index. `store.py:upsert_memory_entry` uses `ON CONFLICT (agent_identity, target, md5(content)) DO NOTHING` matching that index.
- **`hermes memory reset`** only erases built‑in `MEMORY.md`/`USER.md` rows; it does **not** touch `hermes_memory` pgvector/AGE tables or the plugin directory.

## License

MIT
