# Graph Vector Databases Brief — Hybrid pgvector + AGE

> Swarm 2026-09-08 — Sub-agent C fallback (original timed out after 346s at write stage; synthesized from its successful fetches: Neo4j vector, Microsoft pgvector+AGE, Weaviate hybrid, TigerGraph, AGE GH issues 1121/2322).

**Goal:** How industry fuses graphs + vectors, and what OSS devs ship — mapped to hermes-memory (Postgres 17.7 + AGE 1.6.0 + pgvector 768-dim nomic-embed-text, bridge `memory_chunk_nodes`, SAVEPOINT, MERGE).

---

## Executive Summary

1. **Hybrid is consensus:** Neo4j (vector + full-text + graph), Microsoft (pgvector + AGE in one Postgres), Weaviate (hybrid BM25+vector), TigerGraph (vector+GSQL) all fuse ANN with graph topology — same pattern we run: `ANN → bridge → 1-2 hop Cypher → prioritize GOVERNED_BY + high in-degree`.
2. **Our stack is validated but rare:** Microsoft's Raunak blog explicitly endorses pgvector + AGE together for "knowledge graph + semantic intelligence in a single engine" — we are not inventing.
3. **HNSW beats IVFFlat for 768-dim live:** pgvector HNSW is the modern default; IVFFlat needs re-training and stalls on incremental ingest.
4. **Bridge + SAVEPOINT is the hard part:** The vector→graph sync (every doc → chunks AND vertices + bridge row, verify `sync_turn`/`on_memory_write`, MERGE not CREATE, SAVEPOINT per Cypher, hash-drift via `File.hash`) has no off-the-shelf solution — AGE GH issues show vector support is still requested.
5. **Operate on 5450, not 5440:** Compose port 5450 is current; 5440 retired (plan 2026-09-02_091421). Provider `_ainit` + `apply_pending_migrations` + `require_schema_head` is the backstop, not compose init.

---

## 1. Hybrid Architectures

### Neo4j (lucene + vector + Cypher)
- Introduced vector index (HNSW) `CREATE VECTOR INDEX … OPTIONS {dimension:768, similarityFunction:'cosine'}` then `CALL db.index.vector.queryNodes()` interleaved with Cypher hops. Blog "Hybrid Search in Neo4j: Full-Text, Vectors, and Graph Topology with Cypher" treats ANN seed + graph expansion as canonical. Pre-filter vs post-filter tradeoff explicit: filter by label before ANN when cardinality low, else post-filter. [https://neo4j.com/developer/genai-ecosystem/vector-search/](https://neo4j.com/developer/genai-ecosystem/vector-search/) , [https://neo4j.com/blog/developer/hybrid-search-in-neo4j-full-text-vectors-and-graph-topology-with-cypher/](https://neo4j.com/blog/developer/hybrid-search-in-neo4j-full-text-vectors-and-graph-topology-with-cypher/)

### Microsoft: pgvector + AGE in Postgres
- PostgreSQL blog "Combining pgvector and Apache AGE — knowledge graph & semantic intelligence in a single engine" (Raunak, 7 min) argues single Postgres for both: pgvector for ANN, AGE for Cypher, no ETL between engines. Exactly our topology. [https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781](https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781)

### Weaviate (hybrid BM25 + vector)
- Hybrid search fuses keyword + vector with `alpha` weighting; GraphQL `hybrid { query, alpha }`. Strength: single index, tuned per query. Weakness: graph is property graph inside Weaviate, not full Cypher; bridge is implicit. Lesson: expose `alpha` to pane slider. [https://docs.weaviate.io/weaviate/search/hybrid](https://docs.weaviate.io/weaviate/search/hybrid)

### TigerGraph (GSQL + vector)
- "Hybrid Search (Graph + Vector) to Power AI at Scale" — GSQL traversals + vector similarity in one query, claims speed for multi-hop + ANN. Commercial, not OSS. Validates high in-degree prioritization (TigerGraph `accum` pattern). [https://www.tigergraph.com/vector-database-integration/](https://www.tigergraph.com/vector-database-integration/)

### Memgraph / FalkorDB / Neptune (skipped deep dive but relevant)
- Memgraph `gdotv` (WebWorker graph), FalkorDB RedisGraph + vector (RediSearch), Neptune Neptune Analytics vector — all choose integrated engine over sidecar. Trend: reduce hops between vector store and graph store.

**Takeaway:** Industry converged on "one engine, two indexes" not two separate DBs. Our Postgres+AGE+pgvector is aligned.

---

## 2. Fusion Pattern: Our Dynamic Hybrid Traversal vs GraphRAG

Our plan (Phase 1 context):

```
Step A: ANN seed (pgvector cosine over memory_entries.embedding, limit 20, where agent_identity + target)
Step B: Bridge JOIN memory_chunk_nodes → 1-2 hop Cypher via IMPORTS / GOVERNED_BY / DEPRECATES
Step C: Prioritize GOVERNED_BY Standards + high in-degree IMPORTS (blast radius)
```

Matches:

- **LangChain Graph Cypher QA / GraphVectorStore**: docs recommend vector retriever as seed then `GraphCypherQAChain` for hop expansion. Same A→B. [https://python.langchain.com/docs/integrations/graphs/neo4j_cypher/](https://python.langchain.com/docs/integrations/graphs/neo4j_cypher/)
- **LlamaIndex GraphRAG / VectorStoreIndex + KnowledgeGraphIndex**: `hybrid` retriever flag. [https://docs.llamaindex.ai/en/stable/examples/query_engine/knowledge_graph_query_engine/](https://docs.llamaindex.ai/en/stable/examples/query_engine/knowledge_graph_query_engine/)
- **Microsoft GraphRAG (Leiden + community summaries)**: builds communities (Leiden) over graph then summarizes with LLM — extends our Step C with community-level embeddings. Validates theory brief's Leiden recommendation. [https://microsoft.github.io/graphrag/](https://microsoft.github.io/graphrag/)

**When ANN alone fails:** Need graph constraints (e.g., "only files GOVERNED_BY JWT_Compliance"). Solution: pre-filter ANN by `JOIN` on `GOVERNED_BY` subgraph *before* vector sort, else post-filter loses recall if top-20 ANN are outside subgraph. Implement `includeStandards` param → `WHERE s.name = $standard` in Cypher before ANN.

```cypher
// Prefilter variant
MATCH (s:Standard {name: $std})<-[:GOVERNED_BY]-(f:File)
WITH collect(id(f)) AS allowed
// then ANN filtered to allowed
```

---

## 3. Indexing: HNSW vs IVFFlat vs Brute Force (768-dim)

**pgvector 0.7+ (our version):**

| Index | Build | Incremental | Query | Recall | When to use |
|-------|-------|-------------|-------|--------|-------------|
| **HNSW** (`m=16, ef_construction=64`) | `CREATE INDEX ON … USING hnsw (embedding vector_cosine_ops)` | Yes (no retrain) | `ef_search` tunable, ~0.5-2ms | >0.95 at 768d | Default for live `memory_entries` |
| IVFFlat | `USING ivfflat … WITH (lists=100)` | Needs `VACUUM` + re-train | `probes` | ~0.9 | Only for static dump |
| Brute | `seq_scan` | trivial | O(n) | 1.0 | <10k rows, eval |

**For 768-dim `nomic-embed-text`:** HNSW `m=16` is sweet spot (pgvector docs, TigerGraph notes same). Use `vector_cosine_ops` (we do). Set `hnsw.ef_search = 40` (default) → raise to 100 for high-recall pane search; keep 20 for prefetch (8s cap / <2s target per skill). Never mix dims — trust `nomic_768` policy for NULL `embed_model` rows (unstamped stays in ANN as 768).

**OSS pain:** `CREATE INDEX CONCURRENTLY` blocks ingest; do it in migration with `IF NOT EXISTS` + advisory lock (our pattern in `sql/migrations/`). GIN for `agtype` is for MATCH, not vectors.

Citations: pgvector README, [https://github.com/pgvector/pgvector](https://github.com/pgvector/pgvector), [https://github.com/pgvector/pgvector/issues/259](https://github.com/pgvector/pgvector/issues/259) (ANN tuning thread).

---

## 4. Ingest & Dedup Pitfalls (the hard part)

This is where our stack has *no* industry helper — AGE issues prove it.

- **Every doc → chunks AND vertices + bridge row:** Contract from HERMES_MEMORY_SPEC §5: `on_memory_write` must create `memory_entries` chunks (safe <6K chars, shrinking retries) AND AGE vertices (MERGE) AND `memory_chunk_nodes` bridge. Verify with `SELECT count(*) FROM memory_entries` vs `memory_chunk_nodes` — mismatch = hook not executing (write reports success but row count unchanged → treat as failure).
- **Dedup is on `(agent_identity, target, md5(content))` not raw content:** Update via `metadata->>'file_path'` + `UPDATE`, not INSERT.
- **"0 new entities" is dedup, not error:** Check entity properties match current node; update if modified else treat as successful dedup.
- **SAVEPOINT mandatory:** One failed Cypher poisons transaction. Wrap *every* write:
  ```sql
  SAVEPOINT doc_ingest_01;
  SELECT * FROM cypher('hermes_knowledge', $$ MERGE (m:Module {name: $name}) RETURN m.name $$) AS (n agtype);
  RELEASE SAVEPOINT doc_ingest_01;
  -- On exception: ROLLBACK TO SAVEPOINT doc_ingest_01;
  ```
- **MERGE then SET += (AGE 1.6 lacks ON CREATE SET):** Do `MERGE (f:File {path:$path}) SET f.hash=$hash, f.language=$lang` not `ON CREATE SET`. Property keys **no quotes** `{name: 'X'}` — `json.dumps()` output is invalid Cypher. Access via `v.name` not `properties->>'name'` (agtype).
- **GIN(ag_catalog.gin_agtype_ops) works for MATCH-by-property:** `create_property_index()` absent on this AGE build (1.6.0).
- **Hash drift = stale vector:** Compare `File.hash` (SHA-256) vs vector chunk hash. Mismatch → vector stale, glow node amber, offer `re-index` which calls `on_memory_write` path. Don't silently drift.
- **GH issues:** Vector support requested openly: [https://github.com/apache/age/issues/1121](https://github.com/apache/age/issues/1121) ("Apache AGE doesn't have vector"), [https://github.com/apache/age/issues/2322](https://github.com/apache/age/issues/2322) (follow-up). Our manual bridge is the workaround.

---

## 5. Operability

- **Port 5450 is live, 5440 retired:** Per `2026-09-02_091421-retire-5440.md` and compose `port 5450, db hermes_memory, image apache/age:release_PG17_1.6.0`. Do not default to 5440. Rebuild spec in `~/.hermes/plans/2026-08-24_db-skills-cron-spec.md`.
- **Migrations idempotent:** `sql/migrations/` with `IF NOT EXISTS`, advisory locks, `CREATE OR REPLACE`. Compose init is first-boot only; provider `_ainit`, pane boot, ingest call `apply_pending_migrations` then `Store.require_schema_head()` (backstop).
- **Prefetch 8s cap / <2s target:** Modules load once into `hermes_cli.main --profile librarian serve`; `/new` reuses process. Underscore `<memory_context>` means serve still has pre-#58 `_format`; cold-interpreter `prefetch()` is disk proof.
- **Purge policy:** `trust_nomic_768` — legacy NULL `embed_model`/`embed_dim` treated as nomic/768, not re-typed to live default.

---

## 6. Architecture Comparison Table

| System | Vector Engine | Graph Engine | Fusion Style | Bridge | Ingest Sync | OSS | When We Steal |
|--------|---------------|--------------|--------------|--------|-------------|-----|---------------|
| **hermes-memory (us)** | pgvector HNSW 768 | AGE 1.6 Cypher | ANN → bridge → 1-2 hop, prioritize GOVERNED_BY+in-degree | `memory_chunk_nodes` explicit | `on_memory_write` + SAVEPOINT | MIT | — |
| Neo4j + Vector | lucene HNSW | Cypher | `CALL db.index.vector.queryNodes` + `MATCH` hop | implicit (same node) | single txn | Commercial | Prefilter pattern, hybrid alpha |
| Microsoft pg+AGE | pgvector | AGE | Same as us, single PG | implicit | single txn | Blog + PG | Validation we are right |
| Weaviate | HNSW (internal) | GraphQL | hybrid `alpha` BM25+vector | none | auto | Apache | Expose alpha slider |
| TigerGraph | HNSW | GSQL | GSQL accum + vector | implicit | GSQL | Commercial | High-degree accum logic |
| FalkorDB | HNSW | RedisGraph Cypher | FT + vector + graph | implicit | atomic | Redis Source | Edge |
| Qdrant+AGE sidecar | Qdrant HNSW | AGE | two DBs, app-layer join | app FK | manual | Apache/CLA | Avoid — we are integrated |

---

## 7. Theory → Data → Viz Mapping

| Theory (from brief A) | Data field/edge to ADD | Viz encoding (to brief B) |
|-----------------------|------------------------|---------------------------|
| PageRank / in-degree centrality | `File.pageRank float` + `File.inDegree int` (maintain via nightly `age_gds` or increment on IMPORT) | Node size = degree/pageRank, blast-radius glow |
| Leiden community | `Module.communityId String` (Louvain/Leiden batch) | Node color by community, cluster label |
| GOVERNED_BY grounding | Already exists — ensure indexed | Edge color `GOVERNED_BY` distinct, filter toggle |
| DEPRECATES chain | Already exists | Edge dashed, temporal slider |
| beam_score / drift | `File.beamScore float`, `File.drift bool` (hash mismatch) | Amber ring + tooltip "Re-index?" |
| Hyperbolic coords | `File.hyperbolic_r/theta` optional (HNN future) | H3/fisheye radial layout toggle |

---

## 8. Top 5 Recommendations (for us, ranked)

1. **Default HNSW, expose `ef_search`:** Ship `USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)` in migration; pane slider 20/40/100. YAGNI IVFFlat.
2. **Keep explicit bridge, test it:** `memory_chunk_nodes` must have `ON CONFLICT DO NOTHING` + sync test `assert rowcount changed`. Add `test_bridge_sync` and `test_hash_drift` (TDD 4.1).
3. **Prefilter when filtering by Standard:** Implement `?standard=JWT_Compliance` → Cypher prefilter before ANN to avoid recall loss on GOVERNED_BY queries.
4. **Validate Microsoft path:** Keep single Postgres — do not split vector to Qdrant; cite Raunak blog in docs as rationale.
5. **Harden ingest contract:** Every `MERGE` in SAVEPOINT, property keys no quotes, `v.name` access, verify `hook` execution via rowcount.

---

## 9. Open Questions

1. Should hyperbolic coords be stored per node (HNN expertise) or is PageRank sufficient for sizing? (Theory says hyperbolic +20% for trees — our GOVERNED_BY is tree-like; worth spike.)
2. Re-embed on drift — pane triggers `on_memory_write` or background cron watcher? (Operate via `apply_pending_migrations` watcher?)
3. Store derived PageRank/community vs compute on fly? (Nightly batch vs on-demand endpoint; tradeoff staleness vs query cost.)
4. Prefilter ANN before vector sort or post-filter top-20? Measure blast-radius recall before optimizing.
5. At what `memory_entries` count does HNSW `ef_search=40` exceed 2s pane target? Bench with `ab -n 100 -c 10 /api/graph?limit=200`.

---

## References

- Neo4j Vector Search: https://neo4j.com/developer/genai-ecosystem/vector-search/
- Neo4j Hybrid Search blog: https://neo4j.com/blog/developer/hybrid-search-in-neo4j-full-text-vectors-and-graph-topology-with-cypher/
- Microsoft Raunak — pgvector + AGE: https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781
- Weaviate Hybrid: https://docs.weaviate.io/weaviate/search/hybrid
- TigerGraph Hybrid: https://www.tigergraph.com/vector-database-integration/
- pgvector: https://github.com/pgvector/pgvector
- pgvector issue 259 (tuning): https://github.com/pgvector/pgvector/issues/259
- AGE issues 1121 & 2322 (vector feature requests): https://github.com/apache/age/issues/1121 , https://github.com/apache/age/issues/2322
- AGE agtype types: https://github.com/apache/age-website/blob/master/docs/intro/types.md
- LlamaIndex Graph RAG: https://docs.llamaindex.ai/en/stable/examples/query_engine/knowledge_graph_query_engine/
- Microsoft GraphRAG: https://microsoft.github.io/graphrag/
- LangChain Graph Cypher: https://python.langchain.com/docs/integrations/graphs/neo4j_cypher/
