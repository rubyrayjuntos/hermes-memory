> ⚠️ **2026-09-08 Peer review — Path B first, A later: This spec is codebase-ingest scoped (`:File/:Module/:Dependency/:Imports`) — not the conversational-memory graph (`noun`/`semantic_edge`/`beam_score` → `OC-MVP-3/4` + `PANE_INTERACTION_SPEC.md`). Do NOT run Phase 3 as written.** Flow A/GOVERNED_BY, DEPRECATES merge, Standard prefilter, and `File.beamScore` rest on DDL-only labels with zero writers (see `V1__vector_age.sql:57,69,70` vs `ingest.py:9`) and conflated scoring (see `walk.py:76-91`). Mutations stay **501** per `RELEASE_CRITERIA.md OC-PROD-2`. Verified survivor: String-ID for bigint (+ HNSW > IVFFlat, MERGE-then-SET/SAVEPOINT). Path B (conversational graph) is next; Path A (ingest revival) deferred.

---

# Graph Vector RAG Visualize+Modify Pane - Research Swarm Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a visualize+modify pane where the hybrid graph+vector DB is discovered and edited as a graph first - theory shapes data, data shapes visualization.

**Architecture:** Dispatch 3 parallel specialists (Graph Theory, Graph Visualization, Graph Vector DBs) to produce citation-backed briefs in docs/research/, then synthesis workshop to derive first-class graph-discovery UX, then spike a vis-network pane with read+write Cypher (SAVEPOINT-wrapped, MERGE, bridge-table sync) over hermes_knowledge + memory_entries.

**Tech Stack:** Postgres 17.7 + Apache AGE 1.6.0 + pgvector 768-dim (nomic-embed-text), psycopg/asyncpg, vis-network JSON (String IDs), Hermes pane 127.0.0.1:7890

---

## 1. Current Context / Assumptions

Exists:
- Hybrid memory: 127.0.0.1:5450 (apache/age:release_PG17_1.6.0), graph hermes_knowledge + memory_entries + memory_chunk_nodes + conversations
- Labels strict: (:File {path,hash,language}), (:Module {name,path}), (:Dependency {name,kind}), (:Standard {name,description})
- Edges strict: [:IMPORTS], [:IMPLEMENTS], [:GOVERNED_BY], [:DEPRECATES]
- Current viz: fountain.html topology-driven (Concepts inner, Sessions pulled by ABOUT weight, Turns orbit) at ~/.hermes/profiles/librarian/assets/fountain.html
- Prior research: .hermes/plans/2026-08-23_211000-graph-tooling-landscape-research.md - no tool does inspect->navigate->act; vis-network is ours to extend
- L1 Router 4500 chars stays as pointer only

Assumptions to validate in Task 0.2:
- hermes_knowledge queryable; 768-dim vectors present
- vis-network remains renderer (B will challenge vs Cytoscape.js/Sigma.js)
- Local-first browser pane, not Electron/Gephi
- Mutations: SAVEPOINT-wrapped, MERGE not CREATE, bridge-synced, history-preserving

---

## 2. Step-by-Step Tasks

### PHASE 0 - Scaffolding

#### Task 0.1: Create research dir
Files: /home/rswan/Documents/hermes-memory/docs/research/swarm-2026-09-08/.gitkeep and README.md
Commands:
  mkdir -p /home/rswan/Documents/hermes-memory/docs/research/swarm-2026-09-08
  touch /home/rswan/Documents/hermes-memory/docs/research/swarm-2026-09-08/.gitkeep
Verify: ls -la ... Expected .gitkeep, README.md
Commit: git -C /home/rswan/Documents/hermes-memory add docs/research/swarm-2026-09-08/.gitkeep && git commit -m "docs: scaffold swarm-2026-09-08"

#### Task 0.2: Snapshot live metrics (read-only)
File: /home/rswan/Documents/hermes-memory/docs/research/swarm-2026-09-08/live-snapshot.md
Commands:
  psql "postgresql://postgres:***@127.0.0.1:5450/hermes_memory" -c "SELECT 'memory_entries', count(*) FROM memory_entries UNION ALL SELECT 'memory_chunk_nodes', count(*) FROM memory_chunk_nodes;"
  psql ... -c "LOAD 'age'; SET search_path=ag_catalog,public; SELECT * FROM cypher('hermes_knowledge', \$\$ MATCH (n) RETURN labels(n)[0], count(*) \$\$) AS (label agtype, cnt agtype);"
  psql ... -c "LOAD 'age'; SET search_path=ag_catalog,public; SELECT * FROM cypher('hermes_knowledge', \$\$ MATCH ()-[r]->() RETURN type(r), count(*) \$\$) AS (t agtype, cnt agtype);"
Save output to live-snapshot.md

### PHASE 1 - Three Parallel Sub-Agents (ONE delegate_task call, 3 entries)

#### Task 1.1: Graph Theory Brief
File: docs/research/swarm-2026-09-08/graph-theory-brief.md
Delegate context: Graph Theory Specialist - cover centrality (degree/betweenness/eigenvector/PageRank/Katz) blast radius, community (Louvain/Leiden/LabelProp/Infomap), spectral/small-world/scale-free, hyperbolic embeddings/HNNs (user is Hyperbolic GNN+MoE), traversals (BFS/random walks/Node2Vec/GraphSAGE) mapped to Dynamic Hybrid Traversal (ANN seeds -> 1-2 hop Cypher -> prioritize GOVERNED_BY+high in-degree), temporal/deprecation. Deliver: Exec Summary 5 bullets, per-topic definition+why-for-THIS-DB+industry adoption+Cypher snippet+citations (URLs), Theory->Data->Viz table, Top5 ranked recommendations, Open Questions. <4000 words. Verify: grep -c "https://" >=8, wc -l >150

#### Task 1.2: Graph Visualization Brief
File: docs/research/swarm-2026-09-08/graph-visualization-brief.md
Delegate: Visualization Specialist - layouts (force Barnes-Hut, hierarchical, circular, stress, fisheye, 3D), libraries vis-network vs Cytoscape.js vs Sigma.js vs D3 vs Gephi vs KeyLines vs yFiles (bundle size, perf, editing, String ID safety), industry Bloom/Browser/gdotv/Neptune LOD/cluster/expand-on-click, modify interactions (drag-create, double-click delete, lasso, consequence preview, undo), vis-network JSON contract {nodes:[{id:String}], edges:[{from:String}]}, theming var(--foreground). Same deliverable structure + comparison table + Top5 for OUR pane. Verify: grep vis-network and String, >=8 URLs

#### Task 1.3: Graph Vector DB Brief
File: docs/research/swarm-2026-09-08/graph-vector-db-brief.md
Delegate: Vector DB Specialist - hybrids Neo4j vector, Memgraph, FalkorDB, Neptune, Weaviate, Qdrant+Milvus+graph, our pattern pgvector ANN -> bridge -> hop -> prioritize GOVERNED_BY, cite GraphRAG (Microsoft, LlamaIndex, LangChain), indexing HNSW vs IVFFlat 768-dim prefilter vs postfilter, ingest pitfalls (hash mismatch stale, 0 new entities dedup, SAVEPOINT poisoning, MERGE then SET += AGE1.6, GIN properties), operability migrations 5450 vs 5440, GitHub recent pgvector+AGE issues. Same structure + arch table + anti-patterns + mapping beam_score/in-degree to viz. Verify: contains memory_chunk_nodes and SAVEPOINT, >=8 URLs

#### Task 1.4: Gate
Commands: ls -lh docs/research/swarm-2026-09-08/*.md; wc -l graph-*.md; Expected 3 files >100 lines each

### PHASE 2 - Synthesis

#### Task 2.1: synthesis-matrix.md
File: docs/research/swarm-2026-09-08/synthesis-matrix.md
Template sections:
1. What Theory Says We Should Store (Theory | Data Field | Why | Source)
2. What Industry Decided (Pattern | Consensus? | Evidence | Implication)
3. What OSS Devs Are Doing (Repo | What They Do | Steal/Avoid)
4. Graph as First-Class Principles (ranked 5)
5. Discovery Flows: Flow A ANN->IMPORTS->GOVERNED_BY, Flow B blast radius, Flow C hash-drift glow, Flow D DEPRECATES trace, Flow E Louvain cluster summary
6. Scope Decisions V1 WILL (GET /api/graph?limit=200 String IDs, click inspect file_path/hash/Standards, expand 1-hop, drift badge, single modify MERGE/DELETE with SAVEPOINT toast) vs V1 WILL NOT (3D, hyperedge, full Cypher console, collab, ML layout)
Verify: grep Flow A && wc -l >80 with 3 tables

### PHASE 3 - Pane Spec & Spike

#### Task 3.1: pane-spec.md
File: docs/research/swarm-2026-09-08/pane-spec.md
Must include: Visual Contract GET /api/graph returns {nodes:[{id:String,label,group,degree,hash,drift,standards}], edges:[{from:String,to:String,label,weight}]}, Layouts fountain vs force, Inspect side sheet (file_path, language, hash, chunk count JOIN memory_chunk_nodes, Standards, in-degree), Modify (Delete edge, Merge Modules DEPRECATES, Relabel) each SAVEPOINT doc_mod_01 ROLLBACK TO SAVEPOINT on fail, confirmation, Safety (MERGE not CREATE, {name:'X'} no quotes, v.name, stringify bigint)

#### Task 3.2: Spike read API (TDD)
Files: tests/test_graph_pane_api.py (create), src/hermes_memory/graph_api.py (modify)
Test: test_graph_api_returns_string_ids - GET /api/graph?limit=5 assert 200, nodes/edges present, each id is str
Run fail: pytest ... -v Expected FAIL route not defined
Impl skeleton: @router.get("/api/graph") def get_graph(limit=200, hops=1, seed=""): return {"nodes":[{"id":"10133099161583617","label":"AuthService","group":"Module","degree":4}],"edges":[]}
Run pass: pytest ... -v Expected 1 passed
Commit

#### Task 3.3: Spike SAVEPOINT mutation (TDD)
Files: tests/test_graph_mutation.py, src/hermes_memory/graph_api.py
Test: test_merge_modules_creates_deprecates_and_preserves_history POST /api/graph/merge asserts DEPRECATES edge exists and old node still present
Impl must include: SAVEPOINT doc_mod_01; SELECT * FROM cypher('hermes_knowledge', $$ MERGE (mOld:Module {name: $nameOld}) MERGE (mNew:Module {name: $nameNew}) MERGE (mNew)-[r:DEPRECATES]->(mOld) RETURN mNew.name $$) AS (name agtype); RELEASE SAVEPOINT doc_mod_01;
Verify: pytest Before FAIL After PASS

#### Task 3.4: Pane HTML
Files: docs/research/swarm-2026-09-08/pane-preview.html (throwaway) + src/hermes_memory/assets/fountain-modify.html
Must: <div id="mynetwork" style="width:100%;height:600px;border:1px solid var(--border)"></div> + vis-network importmap + JS fetch /api/graph -> vis.DataSet String IDs, color by communityId size by degree drift dashed border, edge opacity label IMPORTS, click -> /api/graph/node/:id side sheet, modify -> POST mutate with toast
Verify: curl -s http://127.0.0.1:7890/api/graph?limit=5 | python3 -m json.tool | head -n 30 Expected JSON ids strings

### PHASE 4 - Validation

#### Task 4.1: Hash-drift badge
Compare File.hash vs memory_entries chunk hash (metadata->>'file_path'), drift:true -> amber ring, button POST /api/graph/reindex calls on_memory_write
Test: test_drift_detection_marks_node

#### Task 4.2: Perf
psql count edges; ab -n 100 -c 10 http://127.0.0.1:7890/api/graph?limit=200 Expected p95 <800ms <2s target

#### Task 4.3: Docs & DoD Gate
Modify docs/specs/HERMES_MEMORY_SPEC.md and README.md screenshot. Gate per DEFINITION_OF_DONE.md Level 6 attach MEDIA: path + curl proof

---

## 3. Tests / Validation
- Every code task TDD: write failing test -> run FAIL -> minimal impl -> run PASS -> commit. Verify with psql row counts and curl JSON, never fabricated.
- Read-only tasks verify grep https and wc -l before gate.
- Final DoD: pytest, curl /api/graph, screenshot pane with String IDs + drift + SAVEPOINT mutation proof (memory_chunk_nodes count)

## 4. Risks, Tradeoffs, Open Questions
Risks: AGE quoting/properties agtype, JS Number precision, vis-network >5k sluggish (gate limit 200 max 500), hash drift whitespace (SHA-256 normalized), transaction poisoning without SAVEPOINT
Tradeoffs: keep vis-network vs Sigma (default keep YAGNI), store PageRank/community vs compute (nightly job), ANN-then-graph vs graph-constrained ANN (start ANN-then-hop)
Open Questions: 1 hyperbolic coords? 2 Concepts/Sessions vs File/Module unify? 3 confirmation vs auto? 4 re-embed owner pane or watcher? 5 when fountain topology beats force-directed?

## 5. Execution Order
1 Phase0 30min real numbers
2 Phase1 3 delegates IN PARALLEL wait for gate 1.4
3 Synthesize 2.1 do not code before merged
4 Spike 3.1->3.4 TDD committed
5 Harden 4.1->4.3 declare Done with media

Frequent commits, DRY, YAGNI, TDD.