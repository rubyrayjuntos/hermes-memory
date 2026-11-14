# Path B Synthesis — Conversational Graph as First-Class Discovery

*Swarm 2026-09-08-B → converges B1 theory + B2 interaction + B3 retrieval into decisions for OC-MVP-3/4. Flagged prior swarm (codebase-ingest) is separate (Path A deferred).*

---

## 1. What Theory Says We Should Store (live fields only)

| Theory | Data (live, file:line) | Why | Viz |
|--------|------------------------|-----|-----|
| Degree centrality | `count(semantic_edge where src/tgt=id)` | Topic anchor, cheap | Node `val=1+degree` size (`_stamp_graph_degree`) |
| Betweenness (sparse) | compute on fly when noun>500 | Bridge entity (rare, high-signal in path graph) | Border thickness on hover if >threshold |
| Leiden community | `noun.leiden_id` (deferred, not yet stored) | Corrects Louvain disconnected communities (Nature) | Color by community when scale warrants |
| beam_score components | `walk.py:76-91` `w,c,decay,score,hop` per edge audit | Only scorer (CORRECTIONS #8) | Edge tooltip magnitude+decay+score ONLY in search (spec §1) |
| provenance_turns | `semantic_edge.provenance_turns` → `conversations_by_ids` `store.py:671` | “Here is why” vs “linked” | Click panel excerpts (280c) |
| drain_status / embed | `conversations.drain_status` `02_schema:21`, `embed_model/dim` NULL→trust_nomic_768 | Honesty per OC-MVP-1/2 | Badges: graph_degraded, unpassported, EMBED_UNSTAMPED |

**Cut:** `File.pageRank/beamScore`, `GovernedBy`, `Deprecates` — all DDL-only or conflated (CORRECTIONS #14), would need pipeline not viz.

---

## 2. What Spec + Industry Decided (Consensus vs Fringe)

| Pattern | Consensus | Evidence | For us |
|---------|-----------|----------|--------|
| Hover identity, no invented score | **Consensus** | Spec §1 + NN/g tooltip guidelines | Keep: label/type/degree only; edge score iff active search payload |
| Click 1-hop with provenance excerpts + explicit empty reasons | Consensus | Spec §2 (5 empty states, never blank) + graph_runtime _anoun_hop | Split #80 raw `#hook` into 4 sections |
| Re-center sequential, not 2-hop render | Consensus | Spec §2 MVP-out multi-hop, 1-hop budget correct for adjacent-chain graph | Enforce: second click re-centers same 1-hop |
| Funnel always visible, from pipeline audit | Consensus | Spec §3 + WalkRow.audit | Footer `ANN→0.55→expand→beam/budget` from pack_search audit, no recompute |
| Single-engine Postgres + HNSW m=16 | Consensus | 03_indexes.sql, pgvector, Microsoft single-engine | Keep HNSW, no IVFFlat migration |
| Leiden > Louvain when clustering | Consensus | Nature 2019 well-connected guarantee | If clustering ships, use Leiden |

---

## 3. What OSS Devs Are Doing (keep / avoid)

| Repo | Do | Steal / Avoid |
|------|----|---------------|
| Microsoft GraphRAG | Leiden communities + LLM summaries over message graphs | Steal pattern after nouns scale; Avoid LLM now |
| Neo4j Browser / Memgraph gdotv | Cypher console + 300-node limit | Avoid console as primary; Steal limit+expand |
| adjacent-pair conversation graphs (this repo) | Path, not mesh | Steal 1-hop MVP; Avoid mesh assumptions |

---

## 4. Graph as First-Class Discovery — Principles (ranked, Path B)

1. **Hover never invents:** No magnitude→score shortcut — no query has no score.
2. **Click proves with text:** 1-hop neighbor list + turn excerpts + provenance badge — not label↔label alone.
3. **Empty states are sourced:** Zero edges / missing turn / graph_degraded / V9-gap / unstamped each has distinct literal from real field (`graph_view.py:504 EMBED_UNSTAMPED` etc.).
4. **Beam is one truth:** `beam_score` is only rank; `HIGH_RELEVANCE` never filters (CORRECTIONS #4), `RAG` quality not MVP gate.
5. **Path, not mesh, guides LOD:** Adjacent chains → 1-hop is sparse; 2-hop render is MVP-out.

---

## 5. Discovery Flows (Graph-First — conversational, these beat vector list)

- **A — Provenance drill:** Search → click noun → see 1-hop edges + provenance_turn excerpts (“these two entities co-occurred because…”) → re-center on neighbor.
- **B — Degraded sweep:** Filter `drain_status='graph_degraded'` or `unpassported` → panel shows incomplete reason, offers `--live` backfill hint (not in-pane mutation).
- **C — Funnel trust:** Search → read footer `ANN→0.55→expand→beam` → knows why kept 2/12 came back.
- **D — Topic anchor:** Sort by degree `val` → pick high-degree noun → 1-hop fans out to session topics.

---

## 6. Scope — B WILL vs WILL NOT (YAGNI, 501)

**B WILL (read-only, MVP):**
- `GET /api/librarian/nouns/{id}/hop` already landed (`graph_runtime._anoun_hop`, `Handler` + `match_route`) — make spec-§2 compliant (4 sections, empty reasons, excerpts).
- `GET /api/librarian/graph/search?q=&k=&hops=1` + funnel from `pack_search` audit; `GET /health` + `/graph/stats` honesty.
- LOD limit 200 (clamped `DEFAULT_LIMIT 250` / `MAX 2000`), re-center flow.

**B WILL NOT (Production):**
- Any `POST/PATCH/DELETE` mutations (stay 501 per `OC-PROD-2`); Cypher studio writes; re-embed triggers in pane; stored `File.beamScore`; `GovernedBy`/`Deprecates` revival; 3D/WebGL.

---

## 7. Open Questions

1. Does `magnitude` weight degree for sizing?
2. At what edge density does LOD truncate neighbor list (50)?
3. Should funnel show `prov_boost` per edge?
4. When does verb_type expand beyond `mentions` (+ `valid_to`)?

*Next: pane-spec-conversational.md for implementer.*
