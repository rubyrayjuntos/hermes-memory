# Architecture — hybrid-age Memory Provider (v0.1)

Tested against Hermes Agent v0.20.x.

## Components

| Module | File (`src/hermes_memory/`) | Responsibility |
|--------|------------------------------|----------------|
| Provider | `provider.py` | `HybridAgeMemoryProvider` implementing Hermes' `MemoryProvider` ABC |
| Config | `config.py` | Resolution order: config.yaml `hybrid_age:` block > env vars > defaults |
| Setup schema | `config_schema.py` | Drives `hermes memory setup hybrid-age` |
| Embeddings | `embed.py` | OpenAI-compatible client (Ollama `nomic-embed-text`, 768-dim invariant; returns None on failure, never a wrong-dim vector) |
| Store | `store.py` (+ mixins) | SQL/Cypher data access — every Cypher statement inside a SAVEPOINT |
| Nouns | `extract_nouns.py` | Live conversation mention extractor (P3/P4) |
| ABOUT (legacy) | `about_concepts.py` | Title-Case cosine linker — backfill / tests only |
| Ingest | `ingest.py` | Codebase indexing after migrate + `Store.require_schema_head` |
| Viz API | `graph_runtime.py` + `graph_http.py` + `graph_server.py` | Live inspector on `:7890` |
| Verify | `verify.py` | End-to-end pipeline smoke-check CLI |

Postgres objects (created by `sql/init/*.sql` on first compose boot):
`conversations`, `messages`, `sessions`, `memory_entries`, `doc_chunks`,
`memory_chunk_nodes`, `librarian_runs(+events)`, `schema_migrations`, plus the
AGE graph `hermes_knowledge` with extractor-ready labels. Extractor columns
(`processed_at`, `relations_processed_at`, `processing_attempts`, `last_error`)
exist now so later extractors can land schema-free. Live conversation writes
use `extract_nouns` + flower/mentions, not the legacy ABOUT cosine path.

## Write Path

```text
sync_turn(user_content, assistant_content, session_id)
    │
    ▼
_enqueue_write({type: "turn", ...})          [returns immediately]
    │
    ▼
Background drain task on a dedicated asyncio loop thread
    │
    ├─► embeddings.create(model=nomic-embed-text, input=text)
    │       │
    │       ▼
    │   768-dim vector → pgvector literal
    │
    └─► INSERT INTO conversations / memory_entries
            │
            ├─► Session/Turn flower MERGE
            ├─► extract_nouns + noun passports
            └─► SQL mentions chain (GRAPH_DEGRADED if AGE/manifold fail)
```

Key points:
- Non-blocking: writes go to an `asyncio.Queue(maxsize=256)`; drops are counted,
  never block the agent loop. Drain records `WriteOutcome` on a process-local
  ledger (in-process counters, not Postgres — a restart zeros them).
- Methods accept `**kwargs` (Hermes adds kwargs regularly); writes only when
  `agent_context == "primary"`.
- Secrets filtered via regex before embedding/write.
- `shutdown()` drains the queue ≤5s and closes the pool.

Write-outcome layers (L0–L6):

| Layer | Functions | Swallow? | Outcome |
|-------|-----------|----------|---------|
| L0 | Hermes `prefetch`, `sync_turn`, `on_memory_write` | Yes — never raise | prefetch `""` + `logger.exception`; writes enqueue or `DROPPED` |
| L1 | Durable SQL `insert_turn`, memory upsert/replace/remove | Catch once in drain | No row (accepted loss) + process `writes_failed`. Nothing to scan or replay. |
| L2 | Embed (`embed_text`, turn embed) | Yes — NULL vector is first-class | Row `drain_status='embed_null'` + process `EMBED_NULL`; still insert SQL |
| L3 | Graph flower / nouns / mentions | Yes after SQL turn (B) | Row `drain_status='graph_degraded'` + process `GRAPH_DEGRADED` |
| L4 | SAVEPOINT MERGE helpers | Yes, savepoint only | `logger.warning` (not debug) with label/edge class |
| L5 | Teardown: shutdown, pane embedder, ghost, taxonomy backfill | Yes | debug or warning; do **not** increment write `FAILED` |
| L6 | `require_schema_head`, bind host | No | raise |

## Recall Path

```text
prefetch(query)
    │
    ▼
Read from pre-warmed cache (queue_prefetch fills it off-thread)
    │
    ├─► Embed query (Ollama)
    ├─► Vector search: memory_entries HNSW, cosine, top-k = vector_k (12)
    │       │
    │       ▼
    │   seeds filtered by min_similarity (0.55) + secret regex
    │
    ├─► Graph expansion via memory_chunk_nodes bridge
    │       └─► 1–2 hop Cypher traversal in hermes_knowledge
    │
    └─► Budget + format → fenced block ≤1200 tokens into system prompt
```

Key points:
- Target <2s warm; Hermes hard-kills prefetch at 8s. Prefetch never raises.
  The return value is `""` for both empty recall and read failure (L0).
  Those are not one Kind: `prefetch empty recall` is a completed search
  with nothing to inject; `prefetch failed` / `prefetch timeout` is the
  read path throwing. No `drain_status` and no write-ledger key for reads.
- Graph expansion scores SQL `mentions` via `beam_score × magnitude`
  (`store_expand` / `walk`), not AGE ABOUT `created_at`.

## Ingest Path

```text
hermes-memory-ingest <path>
    │
    ├─► Walk files, skip artifacts (hex/uuid/digit names, skip-dirs)
    ├─► SHA-hash each file vs state JSON (~/.hermes/cache/doc_ingest_state.json)
    │       └─► unchanged files skipped; modified files flagged as drift
    ├─► Chunk → embed → INSERT doc_chunks (pgvector)
    ├─► Batched MERGE vertices/edges into AGE (≥50/txn)
    └─► Bridge rows in memory_chunk_nodes (vertex_id + graph_name aligned)
```

Re-running over an unchanged repo produces zero new entities (dedup proof).

## Isolation

- `agent_identity` scopes all writes (defaults to session key or profile name).
- Bridge table carries `graph_name` for future multi-graph support.

## AGE-specific behavior

See [age-quirks.md](age-quirks.md). Highlights: savepoint isolation for every
Cypher statement, MERGE-then-SET instead of unsupported `ON CREATE/MATCH SET`
(plan §3.3), GIN containment indexes hit by raw SQL but possibly bypassed by
Cypher-internal `@>`.
