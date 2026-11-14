# Graph layer — research and plan

**Status:** proposal for review. Nothing here is implemented.

**Supersedes in part:** `2026-09-03-conversation-manifold-design.md` §2 (non-goal "LLM extraction in the drain loop") and §6 (rule-based noun extraction). Everything else in that spec stays: relational semantic graph, AGE episodic flower, passports.

## 1. Why this exists

The plugin promises vector recall **and** graph traversal. Multi-hop questions ("where is the owner of X based?") are the reason the graph exists. Today the graph does not answer them.

Measured with `scripts/eval_memory.py` (200 facts, 800 chatter turns, 150 questions, one run):

| Metric | Value |
|---|---|
| Recall, single-fact questions | 1.00 |
| Recall, multi-hop questions | 0.25 |
| Recall, vector search alone | 0.72 (hybrid 0.73: graph adds ~1 point) |
| Unrelated projects per injected answer | ~9 |
| Prefetch latency p50 / p95 | 664 / 816 ms (graph expansion ~600 ms of it) |
| Entities extracted: owners / projects / cities | 200 / 0 / 0 of 200 |

Root causes, from the code and the live schema:

1. **One edge type.** Every `semantic_edge` is `verb_type = 'mentions'` (co-occurrence in a turn). Nothing stores "X is owned by Y" or "Y is based in Z".
2. **Entities the extractor misses do not exist.** Extraction is regex plus a hard-coded project list. Single-word names and places are not minted.
3. **The walk can only cross existing links.** Expansion moves noun → noun over `mentions` edges, at most two hops, starting only from vector hits that already have a passport.
4. **Not owner-scoped.** `noun`, `semantic_edge` and passports have no memory-owner column, and `conversations_by_ids` fetches by id only. Graph expansion can inject another profile's turns, although vector search is scoped.
5. **Typed facts and aliases were designed but never wired.** `claims` (subject, verb, object, polarity, act, valid, superseded_by, span provenance) and `aliases` (surface → canonical id) exist in V12 and have no writer.

## 2. What the research says

**Apache AGE.** The AGE docs have little on hybrid retrieval. The ecosystem pattern is: vector recall in SQL, pass candidate ids into `cypher()` to expand neighbours, fuse the result lists ([Microsoft: pgvector + AGE](https://techcommunity.microsoft.com/t5/microsoft-blog-for-postgresql/combining-pgvector-and-apache-age-knowledge-graph-semantic/ba-p/4508781); [age_search](https://pypi.org/project/age-search/) uses RRF fusion; [langchain-age](https://pypi.org/project/langchain-age/)). Practical cautions for a prompt-time path:

- AGE creates no indexes on new graphs; BTREE on `id` and GIN on `properties` must be added by hand ([Azure AGE performance](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/generative-ai-age-performance)).
- GIN is only used when the filter is inside `MATCH (n {k: v})` braces, not in `WHERE` ([apache/age issue 2137](https://mail-archive.com/dev@age.apache.org/msg07346.html)).
- An open report says the Cypher executor does not use property indexes in 1.7 ([issue 2348](https://mail-archive.com/dev@age.apache.org/msg08099.html)); undirected paths are slow.
- `cypher()` does not take bind parameters, so values must be escaped ([langchain-age](https://pypi.org/project/langchain-age/)).

**Agent-memory graphs.** [Zep / Graphiti](https://arxiv.org/html/2501.13956v1) is the closest match to this plugin:

- An LLM extracts entities from each message (with the previous few messages for context), then resolves them against existing entities using name embeddings plus full-text search, with an LLM deciding duplicates.
- Facts are edges between resolved entities with a relation type, the fact text, a fact embedding, and `valid_at` / `invalid_at`. A contradicting fact invalidates the old edge instead of deleting it.
- Retrieval runs cosine search (facts, entity names), full-text search and breadth-first search in parallel, then reranks (RRF, MMR, mention frequency, graph distance, optional cross-encoder). Context is a compact `<FACTS>` block with date ranges plus an `<ENTITIES>` block, about 1.6k tokens.

**Multi-hop retrieval.** [HippoRAG 2](https://arxiv.org/html/2502.14802) links query entities to graph nodes and spreads activation (personalized PageRank) across phrase and passage nodes, so bridge facts that are semantically far from the query are still reached. [LightRAG](https://www.manning.com/preview/retrieval-augmented-generation-the-foundational-ideas/chapter-12) splits retrieval into an entity-level path and a vector theme path. The recurring caveat: embedding similarity alone misses multi-hop bridges, and noisy LLM extraction produces noisy graphs ([robust GraphRAG](https://arxiv.org/pdf/2603.14828)).

## 3. Design

### 3.1 Principles

- **Vector finds entry points; the graph connects them.** Vector search picks seed turns, facts and entities by meaning. Typed edges supply the bridges that meaning alone misses.
- **Facts, not co-occurrence.** The unit of graph memory is a typed, provenance-carrying fact.
- **Every row is owner-scoped and traceable** to the turn (span) it came from.
- **The prompt path stays relational.** Postgres tables with BTREE, pg_trgm and HNSW indexes, queried with bounded joins or recursive CTEs. AGE stays the episodic flower and becomes an optional mirror for the pane and ad-hoc Cypher, given the index caveats above.

### 3.2 Data model (extend, do not replace)

| Table | Role | Changes |
|---|---|---|
| `noun` → entity | Canonical entity | Add `owner`, `type` (person, project, place, org, tool, concept, …), `name_vec vector(768)`, optional `summary`. Unique on `(owner, canonical)`, not `label` alone |
| `aliases` | Surface → entity | Add `owner`; reference the entity id |
| `claims` | Typed fact edge | Add `owner`, `subject_id`, `object_id` (entity) or `object_text` (literal), `fact_text`, `fact_vec vector(768)`, `valid_at`, `invalid_at`, `extractor`, `confidence`. Keep `span_id`, `act`, `polarity`, `valid`, `superseded_by`, `uptake` |
| `memory_chunk_nodes` | Turn → entity passport | Add `owner` |
| `semantic_edge` | Aggregated entity–entity weight | Add `owner`; derive weight from supporting claims; keep `mentions` as a weak edge type |

Indexes: HNSW on `claims.fact_vec` and `noun.name_vec`; pg_trgm GIN on alias surfaces; BTREE on `(owner, subject_id)`, `(owner, object_id)`, `(owner, verb)`.

### 3.3 Write path (drain, off the prompt path)

1. **Extract** entities and relations from each turn as validated JSON: entities with types, facts as (subject, relation, object or literal, fact text), plus act and polarity. Previous 2–4 turns are given as context, as in Graphiti.
2. **Resolve** each entity: exact alias match, then trigram plus name-embedding candidates within the owner, then merge above a threshold. Record a new alias for each new surface.
3. **Upsert facts**: same (subject, relation, object) adds support and provenance; a contradiction sets `invalid_at` / `superseded_by` on the old fact instead of deleting it.
4. **Embed** fact text and new entity names in batches.
5. **Assistant turns** produce `unconfirmed` facts only. They are never injected as facts unless a user turn confirms them. This addresses the self-reinforcing attribution seen in live testing.

The current in-memory queue (256 items, 5 s shutdown drain) loses writes under bursts. Extraction must run from a durable stage (`drain_status` already exists) so work survives shutdown and can be retried and re-run when the extractor changes.

### 3.4 Read path (prompt time)

1. Embed the query (~20 ms measured).
2. Generate candidates in parallel, each owner-scoped:
   - turn vectors (existing arm),
   - fact vectors (`claims.fact_vec`),
   - entity linking: query surfaces → aliases (exact, trigram) and `name_vec` → seed entities; also the entities of the top turn seeds via passports.
3. **Traverse** typed facts from the seed entities for at most 2 hops with fan-out caps, live facts only, scoring each step by edge confidence × cosine(fact_vec, query) so expansion follows the question instead of drifting. A few iterations of spreading activation over this small subgraph (HippoRAG-style) is an optional upgrade.
4. **Fuse** with RRF across the turn, fact and graph lists; MMR to remove near-duplicates, including the duplicate neighbour spans seen today.
5. **Pack** a compact facts block first (subject – relation – object, date, span id, grounded or unconfirmed), then the supporting turns that fit the budget. Every item carries its span id, and the block lists the ids it contains, so an agent's citation can be checked against what it was actually given.

## 4. Acceptance gates (eval harness)

`scripts/eval_memory.py` grows to measure the graph directly. Proposed targets; adjust before Phase 1 starts:

| Gate | Target |
|---|---|
| Entity coverage on the eval corpus (projects, people, places) | ≥ 0.95 |
| Fact recall against ground-truth relations | ≥ 0.90 |
| Multi-hop recall | ≥ 0.80 (today 0.25) |
| Single-fact recall | ≥ 0.95 |
| Hybrid minus vector-only, multi-hop | Clearly positive; otherwise the graph is not earning its cost |
| Unrelated projects per answer | ≤ 2 (today ~9) |
| Prefetch latency at 10k turns | p50 ≤ 150 ms, p95 ≤ 300 ms |
| Cross-profile leakage | 0 rows from another owner, in vector and graph paths |
| Supersession | A contradicted fact is never injected as current |

Later, for external comparison: LongMemEval or LoCoMo.

## 5. Phases

| Phase | Work | Gate |
|---|---|---|
| 0 | Owner scoping on graph tables and turn fetches; durable extraction stage; remove duplicate neighbour spans; extend the harness with entity, fact, leakage and scale metrics | No cross-profile rows; no burst loss |
| 1 | Entity layer: extractor interface (model-based, rules as fallback), entity types, resolution via aliases + trigram + embeddings | Entity coverage |
| 2 | Fact layer: typed relations into `claims`, fact embeddings, supersession | Fact recall, supersession |
| 3 | Hybrid retrieval: entity linking, fact arm, bounded typed traversal, RRF + MMR, packed block with provenance | Multi-hop, noise, latency |
| 4 | AGE mirror for the pane and Cypher; retire the co-mention-only walk | Pane reads the new graph |
| 5 | Scale and robustness: 10k / 50k turns, re-extraction, backfill of existing history | Latency at scale |

## 6. Decisions needed

1. **Extraction engine.** Recommended: a local model through Ollama with JSON-schema output, rules as fallback. This reverses the manifold spec's non-goal. Alternative: improved rules only, which is cheaper but will not produce typed relations reliably.
2. **Prompt-path engine.** Recommended: relational tables (as today). Alternative: AGE Cypher, subject to the index caveats in §2.
3. **Assistant output.** Recommended: stored as unconfirmed facts, never injected as facts until a user confirms.
4. **Latency budget** for prefetch at 10k turns (proposed p95 ≤ 300 ms).

## 7. Risks

- Model extraction is slower than regex. It must stay off the prompt path and run from a durable stage.
- Extraction noise creates noisy graphs. Resolution thresholds, confidence on facts, and the eval gates are the guard.
- Existing history needs re-extraction; Phase 5 covers backfill.
- The synthetic eval is English and templated. Real-conversation sampling is needed before release claims.

## Changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 0.1 | 2026-11-14 | 2026-11-14 | Claude | Research and plan for a typed, owner-scoped graph layer working with vector retrieval. |
