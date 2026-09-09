> ⚠️ **2026-09-08 Peer review — Path B first, A later: This spec is codebase-ingest scoped (`:File/:Module/:Dependency/:Imports`) — not the conversational-memory graph (`noun`/`semantic_edge`/`beam_score` → `OC-MVP-3/4` + `PANE_INTERACTION_SPEC.md`). Do NOT run Phase 3 as written.** Flow A/GOVERNED_BY, DEPRECATES merge, Standard prefilter, and `File.beamScore` rest on DDL-only labels with zero writers (see `V1__vector_age.sql:57,69,70` vs `ingest.py:9`) and conflated scoring (see `walk.py:76-91`). Mutations stay **501** per `RELEASE_CRITERIA.md OC-PROD-2`. Verified survivor: String-ID for bigint (+ HNSW > IVFFlat, MERGE-then-SET/SAVEPOINT). Path B (conversational graph) is next; Path A (ingest revival) deferred.

---

# Synthesis Matrix — Graph Vector RAG Visualize+Modify

*Swarm 2026-09-08 — converging theory (A), visualization (B), vector DBs (C) into decisions. Theory shapes data, data shapes visualization, graph is first-class discovery.*

---

## 1. What Theory Says We Should Store

| Theory | Data field / edge to add | Why it matters for hermes_knowledge | Viz encoding | Source |
|--------|--------------------------|-------------------------------------|--------------|--------|
| PageRank / Katz | `File.pageRank: float` | Blast radius — hub files break many importers | Node size (gem size = degree today → size = PageRank) | A §1 |
| In-degree | `File.inDegree: int` (maintain incrementally) | Cheaper than PageRank, same blast signal | Badge count + size | A §1, C §7 |
| Betweenness | `File.betweenness: float` | Bridge files that connect clusters | Border thickness | A §1 |
| Leiden community | `Module.communityId: String` | Auto-cluster Modules without manual Standard | Node color by community, cluster label | A §2, C §7 |
| Spectral / small-world | `GraphMeta.smallWorldCoeff` (validate 1-2 hop budget) | Confirms 1-2 hop is enough (theory: L~log n) | LOD threshold selector | A §3 |
| Hyperbolic coords | `File.hyp_r, hyp_theta` (optional) | GOVERNED_BY is tree-like → +20% enrichment (HypSeek) | H3/fisheye radial toggle | A §4, B §3 |
| beam_score | `File.beamScore: float` | Already in LEDGER/drain_status — surface it | Opacity/ring | C §7, A |
| drift | `File.drift: bool` (hash mismatch) | Vector stale signal | Amber ring + tooltip | C §4 |
| GOVERNED_BY | Already exists | High-signal prioritize | Edge color distinct, filter toggle | All |
| DEPRECATES chain | Already exists | Temporal trace, flag contradictions | Dashed edge + slider | A §6 |

---

## 2. What Industry Decided (Consensus vs Fringe)

| Pattern | Consensus? | Evidence | Implication for us |
|---------|------------|----------|--------------------|
| vis-network for <5k nodes, Sigma/Graphology for >10k | **Consensus** | pkgpulse 2026 guide; Memgraph bench Vis 27s vs Sigma 5s at 10k | Keep vis-network, gate `limit` 200 (max 500), server-side community summarization beyond | B |
| Force Barnes-Hut O(n log n) as default, hierarchical for DAGs | Consensus | B §2.1; js.cytoscape docs | Default fountain (topology) + toggle hierarchical `Modules→Files→Deps` | B |
| Expand-on-click with limit (Bloom) | Consensus | Neo4j Bloom scene-interactions: expand local area, select result, expand again | Our pane: click node → side sheet → “Expand +1 hop” button | B |
| Single engine (PG+AGE+pgvector) > sidecar | Consensus | Microsoft Raunak blog; Neo4j vector inside Cypher; TigerGraph | Keep single Postgres 5450, do not split Qdrant | C |
| HNSW > IVFFlat for live 768-dim | Consensus | pgvector README, issue 259 | Default HNSW `m=16`, expose `ef_search` 20/40/100 | C |
| AGE bigint Stringify at API | Consensus | AGE agtype types.md + jsonic `MAX_SAFE_INTEGER` footgun | Hard requirement: `id::text` / Python `str(id)` before JSON, test fails if Number | B |
| Theming via CSS vars | Consensus | yFiles, KeyLines theming docs | Use `var(--foreground)`, `var(--border)` etc. — no hard bg | B |
| 3D/WebGL for >10k aesthetic | Fringe / niche | Stanford H3 hyperbolic, but fisheye complexity high | NOT V1 — YAGNI until >5k pain proven | B |
| GraphRAG Leiden community summaries | Emerging consensus | Microsoft GraphRAG | V2: Leiden batch + LLM summary per community | A,C |

---

## 3. What OSS Devs Are Actually Doing

| Repo / Pattern | What they do | Steal / Avoid |
|----------------|--------------|---------------|
| `apache/age` + `pgvector` (our pattern) | Bridge manually via `memory_chunk_nodes`; AGE issues 1121/2322 show vector requested but not built-in — devs build own FK | Steal: explicit bridge test; Avoid: assuming AGE returns vectors |
| Memgraph gdotv | WebWorker split `graph.worker.js` vs `render.worker.js`, GSS style `gss-lite` | Steal: offload layout to Worker for >200 nodes; Avoid: GSS lock-in |
| Neo4j Browser / Bloom | Cypher input + table+graph toggle, hard 300 node limit default | Steal: limit + expand pattern; Avoid: Cypher console as primary UX |
| Sigma.js examples (graphology) | Load `graphology` graph, run Leiden in Worker, color communities | Steal: Worker Leiden for V2; Avoid: rewrite unless vis chokes |
| Fountain.html current | Topology-driven not force-directed: Concepts inner, Sessions orbit, gem size=degree | Steal: topology for concept graph; For File/Module view, switch to force — topology fails there |

---

## 4. Graph as First-Class Discovery — Principles (ranked)

1. **Seed vector, discover graph:** Every search starts ANN then *shows* graph context — no orphan list without edges. User follows `GOVERNED_BY` and `IMPORTS` visually, not via `WHERE metadata` filters.
2. **Blast radius is a first-class size:** PageRank/in-degree is not metadata — it *is* the gem size and the warning ("2 files IMPORT this Module. Delete anyway?").
3. **Governance is a filter, not a tag:** Click a `(:Standard)` node → see all governed Files/Modules. Standard nodes are hubs in pane, not hidden properties.
4. **Rot glows:** Hash drift is a *visual* amber ring, not a log. Click → "Re-index?" calls `on_memory_write` — theory (drift) → data (hash) → viz (ring).
5. **History survives:** `DEPRECATES` is an edge you *follow*, not an overwrite you *erase*. New node `DEPRECATES` old, old stays traversable. Deprecation trace is a slider.

---

## 5. Discovery Flows (Graph-First — these beat vector search alone)

- **Flow A — Governance drill:** Seed ANN ("JWT auth") → click `JWT_Compliance` Standard → expand 1 hop → see all `GOVERNED_BY` Files/Modules → laser to compliance-relevant subgraph without rewriting query.
- **Flow B — Blast radius:** Click high in-degree Module (in-degree badge 12) → side sheet shows transitive importers (1-2 hop `IMPORTS`) with gutter warning count → prevents breaking change.
- **Flow C — Rot sweep:** Filter `drift:true` → amber ring nodes cluster → lasso → bulk Re-index (calls `on_memory_write` per path, verifies rowcount).
- **Flow D — Deprecation trace:** Follow `DEPRECATES` chain from old `AuthService` → new `AuthServiceV2` → flag contradiction if vector chunk still cites old hash.
- **Flow E — Community dive:** Toggle "Color by community" (Leiden) → 5 clusters → pick largest → pane shows semantic summary of its chunks (future LLM).

---

## 6. Scope Decisions (YAGNI cut — enforce)

**V1 WILL:**
- `GET /api/graph?seed=...&hops=1|2&limit=200&standard=...&includeStandards=true` → `{nodes:[{id:String,label,group,degree,pageRank?,communityId?,drift:Boolean,standards:[String]}], edges:[{from:String,to:String,label,weight}]}` IDs **String**
- Click-to-inspect side sheet: `file_path`, `language`, `hash`, `drift`, chunk count (`JOIN memory_chunk_nodes`), `GOVERNED_BY` Standards, in-degree, pageRank if computed
- Expand +1 hop button; hash-drift amber badge; single modify: `MERGE`/`DELETE` edge or relabel Module with `SAVEPOINT doc_mod_01` + bridge sync + consequence toast (`count()` importers)
- Limit 200 (max 500), p95 <800ms, prefetch 8s cap / <2s target

**V1 WILL NOT:**
- 3D/WebGL/hyperedge editor, full Cypher console, multi-user live collab, ML auto-layout, vector re-embed inside pane (calls `on_memory_write` hook instead), hyperedge creation, community LLM summary (V2)

---

## 7. Open Questions (from briefs — to test in spike)

1. Keep vis-network vs switch to Sigma only if `ab -n 100 /api/graph?limit=200` p95 >800ms or >5k nodes lag proven.
2. Unified vs separate views: Fountain model (Concepts/Sessions) vs File/Module — synthesize via shared `fountain-modify.html` with view toggle, not merge tables.
3. Confirmation policy: high in-degree delete = confirm; relabel low-degree = auto (threshold in-degree ≥5).
4. Re-embed owner: pane triggers `on_memory_write` (simple), background watcher cron is optimization (needs `drain_status`).
5. Hyperbolic coords spike: only if `communityId` clustering not enough for hierarchy visualization.

---

*Next: `pane-spec.md` (Task 3.1) → TDD spikes 3.2-3.4.*