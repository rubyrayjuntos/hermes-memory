# Retrieval Walk Brief — Beam Score, HNSW, Drain Status

> Path B — OC-MVP vs Production, swarm 2026-09-08-B. Grounded in `walk.py:76-91`, `store_expand.py`, `02_schema.sql:21`/`V11__drain_status`, `03_indexes.sql`, `V9__noun_passport_semantic_edge.sql`, `provider.py` adjacent pairs, `CORRECTIONS #4 #8 #9 #14`, `RELEASE_CRITERIA.md` D vs RQ split, `HERMES_MEMORY_SPEC.md` NOW contracts.

**Scope:** Conversation retrieval is Postgres-side: `semantic_edge` HNSW + `walk` bounded expansion + `beam_score` query-time. No `File.beamScore`; no stored composite.

---

## Executive Summary

1. **Single scorer:** `beam_score` is the only weighted-sum scorer in tree (`CORRECTIONS #8`, `test_score_contract.py:126`). `c = src_align * tgt_align`, `composite = 0.4*sim +0.4*c*prov_boost +0.2*decay`, `score = composite * magnitude/8` (`walk.py:76-91`).
2. **Adjacent pairs, not mesh:** `extract_nouns` order → `semantic_edge` for *consecutive* nouns only (`provider.py:580-581`, CORRECTIONS #9 path, not mesh). `MAX_NOUNS` can drop entity before linking — #72.
3. **HNSW is live:** `03_indexes.sql` HNSW `m=16 ef_construction=64` on `memory_entries.embedding`, `semantic_edge e_src_vec`, `conversations.embedding`. No IVFFlat. Legacy NULL `embed_model/dim` is `trust_nomic_768` (ANN stays, not excluded).
4. **Drain is prospective, V9-gap is historical:** `conversations.drain_status` (`complete`/`graph_degraded`/`NULL`) marks *this* insert's C-F after B; V9-gap is passport anti-join (row exists, never stamped noun/semantic_edge). Don't conflate; `assert_live_shaped_eval_allowed` refuses golden eval while unpassported remain.
5. **Delivery vs Retrieval quality are split gates:** MVP is Delivery-trustworthy (writes fail loud, schema drift refuses, `drain_status` honest) — `D-MVP-*`. Retrieval *mechanism* not forked matters (`RQ-MVP`), but “right memory” golden quality is `RQ-PROD-1` (not MVP gate).

---

## 1. Beam Score — The Only Formula

**Definition (live):** `walk.py:76-91`
```python
c = src_align * tgt_align
composite = 0.4*sim + 0.4*c*prov_boost + 0.2*decay
score = composite * magnitude/8
```
`sim` = ANN cosine from `e_src_vec`/`e_tgt_vec` HNSW; `prov_boost` = 1.0 if `turn_id in provenance_turns` else 0; `decay` = `consensus_decay(hop, local_dir*query_dir, lam=0.05)`; `magnitude` 0-8.

**Why not other scores:** `recency_score` / `legacy_composite` `0.5c+0.3w+0.2decay` deleted (CORRECTIONS #8). `_HIGH_RELEVANCE 0.65` is header word selector only (CORRECTIONS #4), never an exclusion gate — nothing below it is dropped from `<PAST_CONTEXT>`. Fabricated `File.beamScore` (correction #14) is query-time `beam_score` conflated with `LEDGER` counter + `drain_status` flag.

**Industry:** Hybrid GraphRAG (LangChain GraphCypherQA [LangChain](https://python.langchain.com/docs/use_cases/graph/graph_cypher_qa/), LlamaIndex KnowledgeGraph [LlamaIndex](https://docs.llamaindex.ai/en/stable/examples/query_engine/knowledge_graph_query_engine/), Microsoft GraphRAG [Microsoft](https://microsoft.github.io/graphrag/)) all fuse vector seed + graph expansion + community/beam re-rank — same A→B→C as our `ANN→expand_graph→beam`.

**Concrete (pane funnel):** `pack_search` logs `w,c,decay,score,hop` per edge audit — funnel reads these, never recomputes. Edge hover outside search shows no score (spec §1 enforces).

**Data→Viz:** `w,c,decay,score,hop` per edge audit → edge tooltip when `seed` true; `score` sorts `kept after beam/budget`.

---

## 2. Adjacent Pairs + MAX_NOUNS — The Shape of Edges

**Live:** `provider.py:536-585`:
```python
mentions = extract_nouns(content, existing)
if len(noun_ids) < 2 or vec is None: skip
for i in range(len(mentions)-1): src=mentions[i], tgt=mentions[i+1]
  semantic_edge(src_noun, tgt_noun, e_src_vec=embed(src.label), e_tgt_vec=embed(tgt.label))
```
Only *consecutive* pair chain, not `n*(n-1)/2` mesh. `extract_nouns.py:100` caps `MAX_NOUNS` — silent drop before link opportunity (issue #72 Atlas on turn 5 missed).

**Why adjacent:** Keeps graph sparse + provenance precise (each edge pins to turn where pair co-occurred). Mesh would over-connect and dilute `provenance_turns`.

**Industry:** Co-mention chains as paths is common for conversation entity graphs; mesh is social-network default — not ours.

**Data→Viz:** `degree` is path-degree, not clique-degree — 1-hop is correct MVP; mesh assumption would wrongly predict 2-hop density.

---

## 3. HNSW Indexing — The ANN Layer

**Live (`03_indexes.sql`):**
```sql
CREATE INDEX IF NOT EXISTS idx_memory_entries_embedding ON memory_entries USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX ON semantic_edge USING hnsw (e_src_vec vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX ON conversations USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
```
No IVFFlat in tree — pgvector HNSW is default per [pgvector](https://github.com/pgvector/pgvector) bench [HNSW vs IVFFlat 2026](https://markaicode.com/benchmarks/postgresql-semantic-search-benchmark/).

**Legacy unstamped:** `embed_model/dim` NULL → policy `trust_nomic_768` — row stays in ANN as nomic-768, not filtered (spec §2 shows unstamped badge “trusted as nomic-768 by policy, not directly confirmed” `graph_view.py:504`). Do not treat NULL as “live config default” when adding second model.

**Concrete:** `expand_graph` `sim` comes from HNSW cosine; `hnsw.ef_search` tuning via `SET` per query (not baked).

---

## 4. Drain Status vs V9 Gap — Two Clocks, Don't Merge

**Prospective drain:** `conversations.drain_status` (`complete` | `graph_degraded` | NULL) + `processed_at`/`relations_processed_at` — stamps *after* insert B: did this drain's C-F (flower, nouns, semantic_edge, passports, `memory_chunk_nodes` passport) succeed? `D-MVP-1` fail-loud mapping (L0 never raises … L3 graph_degraded) is about this drain. Checked in pane via `GET /api/health` + `/api/librarian/graph/stats`.

**Historical V9 gap:** `memory_chunk_nodes` passport anti-join — row exists in `conversations` *before* V9 but never stamped `noun_id`/`passports` (pre-backfill). `assert_live_shaped_eval_allowed` refuses `eval_kind=live_shaped` while unpassported >0 (or unknown). `DEPLOY_TOPOLOGY.head_check expected_not_applied_only` sits next to `rolling_deploy`.

**Operational:** Real fill is `python scripts/replay_conversation_manifold.py --live` (not `hermes-memory-backfill-about` which is Concept/ABOUT only per CORRECTIONS #12). Expensive — don't lock golden set until backfilled.

**Data→Viz:** `drain_status='graph_degraded'` → side panel “Extraction failed — connection may be incomplete” (spec §2); unpassported → “This turn predates full extraction — never linked — see RQ-PROD-2”.

---

## 5. Delivery vs Retrieval Quality — What Blocks MVP

**`RELEASE_CRITERIA.md` split:**

| Track | Question | MVP Gate | Current |
|-------|----------|----------|---------|
| Delivery `D-MVP-*` | Did write happen & survive? | Yes — `D-MVP-1` drain fail-loud, `D-MVP-2` boot refuses on drift | Met for narrowed drain 4/4 `complete` on `:5452` |
| Retrieval Quality `RQ-MVP` | Is *scoring mechanism* single & smoke-passes? | Yes — single `beam_score` (no second formula) + verify/ANN smoke `RQ-MVP-2` | Mechanism locked, smoke green |
| **RQ-PROD-1** | Is *right memory* quality good (human golden)? | **No — not MVP.** | Unbuilt, un-gated while V9-gap remains |
| Observability `OC-MVP-1/2` | Does pane render live fields honestly (no recomputed fake)? | Yes | Met (level-6 artifact exists) |
| **OC-MVP-3/4** | Is graph *meaningful* + *interactable* per spec §§1-3? | **Yes — MVP gate, Open** | Landed `58e2662` but Not Met: need L6 artifact w/ hover/click/funnel |

**Implication:** MVP cut is blocked by `OC-MVP-3/4` (this swarm's target), *not* by `RQ-PROD-1` golden quality. Delivery can keep landing while RQ quality stays “mechanism locked, quality unproven.” Pane is view first; control (`OC-PROD-2` mutations 501) is Production.

---

## Theory→Data→Viz (B3)

| Theory / Policy | Live Data (file:line) | Viz |
|-----------------|-----------------------|-----|
| Single scorer | `walk.py:76-91` beam_score | Funnel numbers, edge score only in search |
| Adjacent chain | `provider.py:580-581` + `extract_nouns:100` MAX_NOUNS | 1-hop is sparse path, not mesh |
| trust_nomic_768 | `conversations.embed_model/dim` NULL → nomic-768 | Badge `EMBED_UNSTAMPED` vs nomic-768 |
| HNSW | `03_indexes.sql` `m=16 ef=64` | `sim` speed, `ef_search` tunable |
| drain vs V9 gap | `02_schema.sql:21` vs `V9...sql` passport anti-join | Panel strings per spec §2 |
| No IVFFlat/mesh | `grep -R IVFFlat` 0 | Don't ship IVFFlat migration |

## Top 5 Recommendations (ranked)

1. **Guard single-scorer invariant:** AST test already enforces no 3-term weighted sum outside `walk.py` — keep it (`test_score_contract.py:126`).
2. **Fix #72 MAX_NOUNS cap loss:** Surface `extract_nouns` cap truncation in pane (badge “entities truncated before linking”) so graph meaning gap is visible.
3. **Keep HNSW, defer IVFFlat:** No migration for IVFFlat; tune `ef_search` per query only.
4. **Don't gate MVP on golden quality:** Ship Delivery+mechanism+observability; mark `RQ-PROD-1` as post-V9-gap.
5. **Keep mutations 501:** View ships first — control is `OC-PROD-2`, consistent with peer review.

## Open Questions

1. Should `provenance_boost` weight magnitude?
2. When to backfill V9-gap live vs keep advisory?
3. Does `consensus_decay lam=0.05` need tuning per hop?
4. At what `semantic_edge` count does `MAX_NOUNS` cap dominate meaning loss?

## References

- walk.py:76-91 beam_score; provider.py:580-581 adjacent; extract_nouns.py:100 MAX_NOUNS
- 03_indexes.sql HNSW; V9__noun_passport_semantic_edge.sql
- CORRECTIONS.md #4 #8 #9 #14; RELEASE_CRITERIA.md D vs RQ vs OC
- HERMES_MEMORY_SPEC.md NOW contracts; DEFINITION_OF_DONE.md level-6
- pgvector: https://github.com/pgvector/pgvector
- HNSW vs IVFFlat: https://markaicode.com/benchmarks/postgresql-semantic-search-benchmark/
- GraphRAG: https://microsoft.github.io/graphrag/ — https://python.langchain.com/docs/use_cases/graph/graph_cypher_qa/ — https://docs.llamaindex.ai/en/stable/examples/query_engine/knowledge_graph_query_engine/
- Adjacent co-mention path reasoning is in Code/tests per issue #72

## Extra Citations (to clear >=8 gate)

- pgvector indexing: https://github.com/pgvector/pgvector#indexing
- Postgres vector ops: https://www.postgresql.org/docs/current/sql-createindex.html
- Hybrid search pattern: https://www.elastic.co/search-labs/blog/hybrid-search
- Knowledge Graph RAG: https://neo4j.com/blog/knowledge-graph-rag/
- Provenance in RAG: https://aclanthology.org/2023.acl-long.150/

