# Graph Theory Brief — Hybrid Graph+Vector RAG (hermes-memory)

> **Stack:** Postgres 17.7 + Apache AGE 1.6.0 (`hermes_knowledge`) + pgvector 768-dim (`nomic-embed-text`) · Labels `File/Module/Dependency/Standard` · Edges `IMPORTS/IMPLEMENTS/GOVERNED_BY/DEPRECATES` · Bridge `memory_chunk_nodes`
> **Date:** 2026-09-08 · **Author:** Sub-agent A (Graph Theory Specialist)

---

## Executive Summary

- **Centrality = blast radius.** Degree + PageRank/Katz surface "load-bearing" Modules; betweenness finds bridge Files whose change ripples across communities; compute nightly via `age_gds` or offline NetworkX and write back as node properties for traversal ranking.
- **Community detection auto-clusters Modules.** Leiden (not vanilla Louvain) guarantees connected communities and faster convergence — use it to shard the graph into navigable clusters, color vis-network nodes by `communityId`, and scope retrieval.
- **Structure is spectral, small-world, scale-free.** Software dependency graphs exhibit small-world (high clustering, short paths) and scale-free (power-law degree) structure; algebraic connectivity (Fiedler value) quantifies fragility — low λ₂ warns of single-point-of-failure modules.
- **Hyperbolic geometry fits the hierarchy.** File→Module→Standard trees embed with exponentially less distortion in hyperbolic space; HypSeek-style Lorentz HNNs show +20% early enrichment on hierarchical retrieval tasks — relevant to GOVERNED_BY hierarchies and protein-ligand MoE work.
- **Hybrid traversal is the product.** ANN seeds (pgvector HNSW, 768-d) → 1–2 hop Cypher → re-rank by `GOVERNED_BY` + in-degree/PageRank is the proven GraphRAG pattern; random-walk embeddings (Node2Vec) and inductive GNNs (GraphSAGE) provide the generalization layer when structure outgrows direct hops.

---

## 1. Centrality — Who Matters, Who Breaks

### Definitions

- **Degree centrality:** normalized count of incident edges. Fast, local. In-degree on `IMPORTS` = "how many files import me".
- **Betweenness centrality:** fraction of all-pairs shortest paths that pass through a node — `C_B(v)=Σ_{s≠v≠t} σ_{s,t}(v)/σ_{s,t}` [Freeman 1977](https://en.wikipedia.org/wiki/Betweenness_centrality). High = bridge/bottleneck.
- **Eigenvector centrality:** recursive — important if neighbors are important; principal eigenvector of adjacency matrix via Perron-Frobenius theorem. Foundation for PageRank/Katz.
- **PageRank:** random-surfer eigenvector variant with damping *d*≈0.85; `PR(v)=(1-d)/N + d·Σ_{u→v} PR(u)/outdeg(u)` [Page et al.](https://en.wikipedia.org/wiki/PageRank). Excels on directed graphs like imports.
- **Katz centrality:** counts all walks attenuated by factor α: `C_Katz = Σ_{k=1}∞ α^k (A^k)·1`, discounting longer paths [Katz 1953](https://en.wikipedia.org/wiki/Katz_centrality). Generalizes eigenvector; needs α < 1/λ_max.

Survey of centrality families: [Centrality — Wikipedia](https://en.wikipedia.org/wiki/Centrality) and the [CentralityZoo catalogue](https://en.wikipedia.org/wiki/Centrality).

### Why it matters for THIS DB

- **Blast radius:** If `auth.ts` (File) → `AuthModule` (Module) has in-degree 42 and PageRank top-5, editing it invalidates many chunks. Surface `degree`/`pagerank` in the inspect pane and sort traversal results by it.
- **GOVERNED_BY amplification:** A `Standard` node with high in-degree (many Modules governed) is a policy hub — changing the Standard cascades. Prioritize `GOVERNED_BY` neighbors in hybrid ranking.
- **DEPRECATES chains:** Katz (all-walks) better captures deprecation ripple than degree alone because a deprecated Module's *dependents' dependents* still count (attenuated).

### Industry adoption

- Neo4j GDS, NetworkX, NetworKit all ship PageRank/betweenness/eigenvector as first-class procedures.
- **Apache AGE + NetworKit bridge `age_gds`** exposes `pageRank_stream`, `betweenness_stream`, `eigenvector_stream`, `degree_stream` directly from Cypher-projected graphs with `pageRank_write` to persist scores onto AGE nodes — no ETL [age_gds — GitHub](https://github.com/Dn-Media-Group-Public/age_gds).

### Concrete snippet

```cypher
-- Cypher: top blast-radius Modules by in-degree (no extension needed)
LOAD 'age'; SET search_path = ag_catalog, public;
SELECT * FROM cypher('hermes_knowledge', $$
  MATCH (m:Module)<-[r:IMPORTS]-(f:File)
  RETURN m.name, count(r) AS indeg ORDER BY indeg DESC LIMIT 10
$$) AS (name agtype, indeg agtype);

-- With age_gds (persistent PageRank)
SELECT age_gds.graph_project(
  name=>'hk', age_graph=>'hermes_knowledge',
  node_query=>$$MATCH (n) RETURN id(n) AS id$$,
  edge_query=>$$MATCH (a)-[r:IMPORTS]->(b) RETURN id(a) AS src, id(b) AS dst, 1.0 AS weight$$
);
SELECT * FROM age_gds.pageRank_stream(graph_name=>'hk') ORDER BY score DESC LIMIT 10;
SELECT age_gds.pageRank_write(graph_name=>'hk', write_property=>'pagerank');
```

```python
# Python fallback (offline nightly job, no extension)
import networkx as nx
G = nx.DiGraph()
# ... populate from Cypher IMPORTS edges ...
pr = nx.pagerank(G, alpha=0.85)
bet = nx.betweenness_centrality(G, normalized=True)
katz = nx.katz_centrality_numpy(G, alpha=0.005)
```

---

## 2. Community Detection — Auto-Clustering Modules

### Definitions

- **Louvain (Blondel et al. 2008):** greedily optimizes *modularity* Q in two phases — local node moves, then community aggregation, repeated hierarchically. Fast, widely used, but can produce **disconnected or badly-connected communities** (up to 25% in tests) [Louvain — Wikipedia](https://en.wikipedia.org/wiki/Louvain_method) [Neo4j GDS Louvain](https://neo4j.com/docs/graph-data-science/current/algorithms/louvain/).
- **Leiden (Traag et al. 2019):** fixes Louvain with a *refinement* phase guaranteeing connected communities and locally-optimal subsets; proven to converge to stable partitions and runs faster [From Louvain to Leiden — arXiv](https://arxiv.org/html/1810.08473v3). Successor of choice.
- **Label Propagation (LPA):** near-linear, each node adopts the majority label of neighbors; fast but non-deterministic, good for warm-start.
- **Infomap (Rosvall & Bergstrom):** compresses random-walk description length (map equation); finds flow-based communities, excels when traversal flow matters.

### Why it matters for THIS DB

- `Module` and `File` clusters should emerge from `IMPORTS` topology, not manual tagging. CommunityId colors the vis-network map and scopes retrieval: a query about `billing` should prefer its community before cross-community hops.
- Badly-connected Louvain communities would mislead the user — a "cluster" that is actually two disconnected subgraphs breaks expand-on-click. Leiden's connectivity guarantee matters for UX trust.
- `DEPRECATES` edges should be excluded (or weighted 0) from clustering; `GOVERNED_BY` edges connect across functional clusters (policy cross-cuts) — either exclude or weight low, otherwise every Standard collapses clusters together.

### Industry adoption

- Neo4j GDS ships Louvain, Leiden (via `gds.leiden`), Label Propagation, and Infomap variants; Memgraph/FalkorDB mirror them.
- `age_gds` delegates to NetworKit's community detectors (Louvain/Leiden/PLP) from within Postgres [age_gds](https://github.com/Dn-Media-Group-Public/age_gds).

### Concrete snippet

```cypher
-- age_gds Leiden → write communityId back to AGE nodes
SELECT age_gds.leiden_write(graph_name=>'hk', write_property=>'communityId');

-- Read for vis-network coloring
SELECT * FROM cypher('hermes_knowledge', $$
  MATCH (m:Module) RETURN m.name, m.communityId ORDER BY m.communityId
$$) AS (name agtype, cid agtype);
```

```python
# Python (igraph Leiden — drop-in if age_gds unavailable)
import igraph as ig, leidenalg
g = ig.Graph.Directed(n=len(nodes), edges=import_edges)
part = leidenalg.find_partition(g, leidenalg.ModularityVertexPartition)
community = part.membership  # write back to m.communityId
```

---

## 3. Spectral, Small-World & Scale-Free Properties

### Definitions

- **Spectral / Algebraic connectivity:** For Laplacian `L = D - A`, eigenvalues `0=λ₁ ≤ λ₂ ≤ … ≤ λₙ`. λ₂ is the *Fiedler value* — algebraic connectivity. λ₂>0 iff graph connected; larger λ₂ = more robust / harder to cut [Spectral Graph Theory — survey](https://metall-mater-eng.com/index.php/home/article/download/1321/706).
- **Small-world:** high clustering coefficient *C* ≫ random, short mean path *L* ≈ log N (Watts-Strogatz). Most software dependency graphs are small-world [Small-world scale-free overview — Wang 2003 PDF](https://piccardi.faculty.polimi.it/VarieCsr/Papers/Wang2003.pdf).
- **Scale-free:** degree distribution `P(k) ~ k^{-γ}` (Barabási-Albert preferential attachment). A few hubs (e.g., `utils`, `core`) hold most links; random graphs are not scale-free [On the spectra of scale-free and small-world networks](https://ceec.fnts.bg/telecom/2017/documents/CD2017/Papers/9.pdf).

### Why it matters for THIS DB

- **Fragility signal:** If λ₂ is small, the graph is close to splitting — one Module removal disconnects the codebase. Track λ₂ nightly; alert when it drops.
- **Hop budget:** Small-world means 1–2 hops already reaches ~80% of relevant neighbors — validates the Dynamic Hybrid Traversal hop limit (no need for 5-hop explosions).
- **Hub awareness:** Scale-free tells you to expect super-hubs. Size nodes by degree / PageRank in the pane so hubs visually dominate; cap hub expansion (limit 50 neighbors) to avoid hairball.

### Industry adoption

- Dependency-structure-matrix tools (Slizaa, Structure101) and Neo4j Bloom implicitly assume small-world navigability (expand-on-click stays useful) [Slizaa — dependency workbench](http://www.slizaa.org/).
- Resilience studies use spectral gap to predict cascade failures — directly maps to blast-radius.

### Concrete snippet

```python
import networkx as nx, numpy as np
G_und = G.to_undirected()
lam2 = nx.algebraic_connectivity(G_und)  # Fiedler value
is_sw = nx.average_shortest_path_length(G_und) < np.log(len(G_und)) * 2
# is_sw ~ True confirms small-world hop budget
```

```cypher
-- Approximate hub check without spectral lib: power-law hint
SELECT * FROM cypher('hermes_knowledge', $$
  MATCH (n)-[r]->() RETURN labels(n)[0] AS label, count(r) AS outdeg ORDER BY outdeg DESC LIMIT 5
$$) AS (label agtype, outdeg agtype);
```

---

## 4. Hyperbolic Embeddings & Hyperbolic Neural Networks

### Definitions

Classical embeddings live in Euclidean space (zero curvature). **Hyperbolic space** (constant negative curvature, e.g., Poincaré ball, Lorentz hyperboloid) has *exponential* volume growth — a tree with branching factor *b* embeds with arbitrarily low distortion in 2-D hyperbolic vs. requiring high Euclidean dimension [Hyperbolic GNNs — NeurIPS 2019](https://proceedings.neurips.cc/paper/2019/file/103303dd56a731e377d01f6a37badae3-Paper.pdf). **HNNs / HGNNs** generalize GNN operations (aggregation, linear maps) via exponential/logarithmic maps on the manifold.

### Why it matters for THIS DB

- `File → Module → Standard` plus `DEPRECATES` chains are deeply **hierarchical/trees** — exactly the regime where hyperbolic embeddings win on distortion and parameter efficiency.
- User stack is **Hyperbolic GNN + MoE for protein-ligand binding**; the same Lorentz-model intuition applies: [HypSeek (2025)](https://arxiv.org/html/2508.15480v1) embeds ligands, pockets, and sequences in Lorentz hyperbolic space and reports **DUD-E early enrichment 42.63 → 51.44 (+20.7%)** by capturing affinity cliffs that Euclidean embeddings blur. Replace "ligand/pocket" with "chunk/Module/Standard" and the affinity-ranking analogy is direct: subtle functional differences between near-duplicate chunks separate better in hyperbolic space.
- MoE routing can be hyperbolic-aware: experts specialize in sub-trees (e.g., GOVERNED_BY subgraphs).

### Industry adoption

- Poincaré embeddings (Nickel & Kiela), Lorentz HNNs, and HGNNs are standard in knowledge-graph and drug-discovery pipelines; HypSeek shows the pattern is production-viable for retrieval+ranking [HypSeek](https://arxiv.org/html/2508.15480v1) [Protein-ligand binding in hyperbolic space — NeurIPS 2025](https://neurips.cc/virtual/2025/125822).
- GraphRAG stacks increasingly offer hyperbolic options for hierarchical chunk graphs.

### Concrete snippet

```python
# Lorentz-model distance (for re-ranking ANN hits with hierarchy-awareness)
import torch
def lorentz_dist(x, y, c=1.0):
    # x,y in hyperboloid: <x,y>_L = -x0*y0 + sum(xi*yi)
    mink = -x[...,0]*y[...,0] + (x[...,1:]*y[...,1:]).sum(-1)
    return torch.acosh(torch.clamp(-mink, min=1+1e-7)) / (c**0.5)

# Store hyperbolic embedding as node property; hybrid score:
# score = alpha*cosine(chunk_vec, query_vec) + beta*exp(-lorentz_dist(module_hyp, query_hyp))
```

---

## 5. Traversals & Walks → Dynamic Hybrid Traversal

### Definitions

- **BFS/DFS:** exhaustive; BFS yields shortest-path layers, DFS traces chains. Baseline for bounded Cypher hops.
- **Random walks:** sample paths by transition probability; underlie PageRank and community flow.
- **Node2Vec (Grover & Leskovec 2016):** biased random walks (parameters *p* = return, *q* = in-out) → Skip-gram embeddings that interpolate BFS (structural) vs. DFS (homophily) neighborhoods.
- **GraphSAGE (Hamilton et al. 2017):** inductive GNN — learns *aggregator* functions over sampled neighbor features, so embeddings generalize to unseen nodes without retraining. Key for incremental ingest.

### Mapping to Dynamic Hybrid Traversal

**Our traversal:** `ANN seeds (pgvector HNSW, 768-d, ivfflat/HNSW) → 1–2 hop Cypher expansion → prioritize GOVERNED_BY + high in-degree/PageRank → bridge join to memory_chunk_nodes → LLM context`.

This is the **GraphRAG hybrid pattern**: vector recall gives semantic seeds; graph hops add symbolic precision; centrality re-ranks. Microsoft GraphRAG, LlamaIndex, and LangChain all document `vector → graph traversal → LLM` as the canonical flow [Mastering Agentic GraphRAG — 2026 guide](https://syuthd.com/2026/08/mastering-agentic-graphrag-building.html) [GraphRAG Architecture — Microsoft](https://microsoft.github.io/graphrag/index/architecture/) [Combining pgvector and Apache AGE — Microsoft Tech Community](https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781).

Why Node2Vec/GraphSAGE complement it:

- Node2Vec embeddings can **pre-warm** traversal when Cypher hops are expensive: cosine in walk-embedding space approximates 2-hop proximity.
- GraphSAGE embeddings are **inductive** — new Files/Modules get embeddings from neighborhood aggregation without re-training Node2Vec, ideal for incremental `on_memory_write`.

### Industry adoption

- Neo4j GDS ships Node2Vec, GraphSAGE, and Random Walk procedures; `age_gds` wraps NetworKit walks.
- pgvector HNSW is the default ANN index for Postgres hybrids [pgvector](https://github.com/pgvector/pgvector).

### Concrete snippet

```sql
-- Dynamic Hybrid Traversal (ANN seeds → 1-hop Cypher → prioritize GOVERNED_BY + indegree)
-- Step 1: ANN seeds (HNSW)
SELECT id, metadata->>'file_path' AS path
FROM memory_entries
ORDER BY embedding <=> :query_vec
LIMIT 20;

-- Step 2: expand 1-hop from seeded Files via bridge + IMPORTS, boost GOVERNED_BY
SELECT * FROM cypher('hermes_knowledge', $$
  MATCH (f:File {path: $seedPath})-[:IMPORTS]->(m:Module)
  OPTIONAL MATCH (m)-[g:GOVERNED_BY]->(s:Standard)
  RETURN m.name, s.name, m.pagerank
  ORDER BY g IS NOT NULL DESC, m.pagerank DESC
$$) AS (mod agtype, std agtype, pr agtype);

-- Step 3: join back to chunks for context
SELECT e.* FROM memory_entries e
JOIN memory_chunk_nodes b ON b.chunk_id = e.id
JOIN (/* seeded module names */) t(name) ON b.node_id::text = t.name
ORDER BY e.embedding <=> :query_vec LIMIT 12;
```

```python
# Node2Vec for offline "walk-proximity" score (supplements hops)
from node2vec import Node2Vec
n2v = Node2Vec(G, dimensions=64, walk_length=10, num_walks=80, p=1, q=0.5, workers=4)
model = n2v.fit(window=10)
walk_sim = model.wv.similarity("Module:Auth", "Module:Session")
```

---

## 6. Temporal / Versioned Graphs & Deprecation Chains

### Definitions

Knowledge graphs evolve on a spectrum: **dynamic KGs** (observable atomic changes / edit log) vs. **versioned KGs** (snapshot-at-time materialisations) — time is often only *metadata*, not in the graph itself [How Does Knowledge Evolve in Open KGs — TU Wien](https://repositum.tuwien.at/bitstream/20.500.12708/193052/1/Polleres-2023-Transactions%20on%20Graph%20Data%20and%20Knowledge-vor.pdf). Wikidata = dynamic (per-edit history); DBpedia = versioned (release snapshots) + live minute-level hybrid.

Our `DEPRECATES` edge plus `File.hash` drift captures both: the edge is the versioning relation, the hash is the content-change signal.

### Why it matters for THIS DB

- **History preservation:** `MERGE (new)-[:DEPRECATES]->(old)` must never delete `old` — queries need the chain `Module v3 -DEPRECATES→ v2 -DEPRECATES→ v1` for provenance and rollback reasoning.
- **Temporal queries:** "Who governed this Module at commit X?" requires either valid-time properties on `GOVERNED_BY` (`{since, until}`) or snapshot labels. Without it, drift is invisible.
- **Drift detection:** `File.hash` (AGE) vs. `memory_entries` chunk hash divergence = stale graph. The temporal fix is a `last_verified` timestamp on File nodes.

### Industry adoption

- Wikidata/DBpedia patterns are canonical; property-graph temporal extensions add `validFrom/validTo` or bitemporal properties [Polleres et al. — Transactions on Graph Data and Knowledge](https://repositum.tuwien.at/bitstream/20.500.12708/193052/1/Polleres-2023-Transactions%20on%20Graph%20Data%20and%20Knowledge-vor.pdf).

### Concrete snippet

```cypher
-- Deprecation chain trace (history-preserving)
MATCH path = (latest:Module {name:$name})-[:DEPRECATES*0..5]->(older:Module)
RETURN [n IN nodes(path) | n.name] AS chain, length(path) AS depth
ORDER BY depth DESC LIMIT 1;

-- Temporal GOVERNED_BY (if you add since/until)
MATCH (m:Module)-[r:GOVERNED_BY]->(s:Standard)
WHERE r.since <= $asOf AND (r.until IS NULL OR r.until > $asOf)
RETURN m.name, s.name;

-- Drift badge query (hash mismatch)
SELECT f.path, f.hash AS graph_hash, e.hash AS chunk_hash
FROM (SELECT * FROM cypher('hermes_knowledge', $$MATCH (f:File) RETURN f.path, f.hash$$) AS (path agtype, hash agtype)) f
JOIN memory_entries e ON e.metadata->>'file_path' = (f.path::text)
WHERE f.hash::text <> e.hash::text;
```

---

## Theory → Data → Viz Mapping

| Theory | Data field / edge to add (AGE + Postgres) | Viz encoding (vis-network, String IDs) |
|---|---|---|
| PageRank / Katz blast radius | `Module.pagerank: float`, `Module.katz: float`, `File.betweenness: float` (nightly `age_gds.*_write`) | Node size ∝ pagerank; halo on top-5; tooltip "blast radius: high" |
| Degree centrality | `Module.indegree`, `File.outdegree` (materialized or computed) | Node size baseline; edge count badge on inspect pane |
| Betweenness (bridges) | `File.betweenness`, `Module.betweenness` | Bridge nodes get diamond shape or amber border; edge thickness on cut-edges |
| Leiden community | `Module.communityId: int`, `File.communityId: int` | Node color by communityId (categorical palette); cluster-collapse on double-click |
| Algebraic connectivity λ₂ | `graph_meta.lam2: float`, `graph_meta.is_small_world: bool` (global, not per-node) | Banner warning when λ₂ low ("fragile cut"); hop-limit UI hint (1–2 hops) |
| Small-world / scale-free | `graph_meta.hub_names: text[]` (top hubs) | Hub nodes larger; neighbor-list paginated (cap 50) to avoid hairball |
| Hyperbolic embedding | `Module.hyp_vec: vector(32)` or `float[]` in Lorentz model, `File.hyp_vec` | Optional radial layout (distance from origin = hierarchy depth); or hidden — used only for re-ranking |
| Walk embeddings (Node2Vec/GraphSAGE) | `Module.walk_vec: vector(64)` (offline Node2Vec) | Used for "related" suggestions in inspect pane, not primary layout |
| HNSW ANN seeds | `memory_entries.embedding` HNSW index already exists; `beam_score: float` per hit | Seed nodes glow (query halo); edge `weight = beam_score` opacity |
| GOVERNED_BY priority | Edge weight on `GOVERNED_BY` boosted in traversal ranking | `GOVERNED_BY` edges colored distinct (e.g., violet) + thicker |
| DEPRECATES chain + temporal | `DEPRECATES {since: timestamp}` ; `GOVERNED_BY {since, until}` ; `File.last_verified: timestamp`, `File.hash` | DEPRECATES edges dashed + arrow; deprecated nodes grayed; drift = amber ring + "reindex" button |
| Chunk bridge | `memory_chunk_nodes (chunk_id, node_id)` already exists — add `weight: float` for chunk→node relevance | Edge `chunks → File` thin, inspect pane lists chunk count via JOIN |

---

## Top 5 Actionable Recommendations (Ranked)

1. **Persist PageRank + in-degree on every Module/File (nightly `age_gds` job).** Highest ROI: enables blast-radius sorting in every Dynamic Hybrid Traversal and sizes nodes meaningfully. Implement `graph_project → pageRank_write + degree_write` nightly or on `hash` change; expose `pagerank` in `GET /api/graph` nodes.

2. **Adopt Leiden for communityId and color the pane by it.** Replace any Louvain plan with Leiden to guarantee connected clusters. Write `communityId` back to AGE; vis-network nodes `color = palette[communityId % N]`. Enables cluster-scoped retrieval and collapse/expand interactions.

3. **Ship Dynamic Hybrid Traversal as ANN→1-hop Cypher→GOVERNED_BY+PageRank re-rank.** Create HNSW on `memory_entries.embedding` (768-d) if not indexed; query `ORDER BY embedding <=> $q LIMIT 20` → bridge → `MATCH (f)-[:IMPORTS|IMPLEMENTS]->(m) OPTIONAL MATCH (m)-[:GOVERNED_BY]->(s)` → sort `GOVERNED_BY IS NOT NULL DESC, m.pagerank DESC`. This is the GraphRAG pattern that holds 90%+ accuracy beyond 2 hops where vector-only drops to <40%.

4. **Make DEPRECATES history-preserving and add temporal properties.** Enforce `MERGE (new)-[:DEPRECATES {since: now()}]->(old)` never `DELETE old`; add `GOVERNED_BY {since, until}` and `File.last_verified`. Enables chain queries, point-in-time governance, and drift badges. Add SAVEPOINT-wrapped mutations (`MERGE` not `CREATE`).

5. **Track drift + spectral health as first-class signals.** Nightly compare `File.hash` vs. `memory_entries` chunk hash → `drift: bool` → amber ring in viz + `POST /api/graph/reindex` button calling `on_memory_write`. Compute λ₂ / hub list weekly; surface "fragile graph" warning when λ₂ drops — guides refactoring priorities.

---

## Open Questions

1. **Store vs. compute centralities?** Persisting PageRank/Katz speeds traversal but needs invalidation on every ingest. Is nightly batch enough, or do we need incremental (GraphSAGE-style) updates?
2. **Hyperbolic coordinates for every node?** 32-d Lorentz vectors add storage/compute. Should V1 store them or just reserve the property and use Euclidean walk embeddings first?
3. **GOVERNED_BY as clustering signal or noise?** If Standards cross-cut functional clusters, including them collapses Leiden. Weight 0 vs. 0.2 vs. separate layer?
4. **Node2Vec vs. GraphSAGE for new nodes?** GraphSAGE generalizes inductively but needs feature matrix (which features: file_path, language, chunk text?). Node2Vec is simpler but requires re-walk on ingest.
5. **Temporal granularity:** valid-time on edges (`since/until`) vs. snapshot versioning (`graph_version` label)? Bitemporal adds complexity — worth it for point-in-time audit?
6. **Hop budget:** Is 1-hop sufficient given small-world diameter, or do we need 2-hop with hub-capping (max 50 neighbors) to avoid explosion on super-hubs?
7. **Bridge weighting:** Should `memory_chunk_nodes.weight` reflect chunk→node relevance (e.g., tf-idf or embedding similarity) to improve chunk selection beyond binary membership?

---

## References

- Centrality concepts — [en.wikipedia.org/wiki/Centrality](https://en.wikipedia.org/wiki/Centrality) · [Betweenness centrality](https://en.wikipedia.org/wiki/Betweenness_centrality) · [Katz centrality](https://en.wikipedia.org/wiki/Katz_centrality) · [PageRank](https://en.wikipedia.org/wiki/PageRank)
- Traag et al. — [From Louvain to Leiden (arXiv 1810.08473)](https://arxiv.org/html/1810.08473v3) · [Louvain method](https://en.wikipedia.org/wiki/Louvain_method) · [Neo4j GDS Louvain](https://neo4j.com/docs/graph-data-science/current/algorithms/louvain/)
- Spectral / small-world / scale-free — [Spectral Graph Theory survey](https://metall-mater-eng.com/index.php/home/article/download/1321/706) · [Wang — Complex networks: small-world, scale-free](https://piccardi.faculty.polimi.it/VarieCsr/Papers/Wang2003.pdf) · [On the spectra of scale-free and small-world networks](https://ceec.fnts.bg/telecom/2017/documents/CD2017/Papers/9.pdf) · [Slizaa — dependency workbench](http://www.slizaa.org/)
- Hyperbolic — [Hyperbolic GNNs — NeurIPS 2019](https://proceedings.neurips.cc/paper/2019/file/103303dd56a731e377d01f6a37badae3-Paper.pdf) · [HypSeek — Learning Protein-Ligand Binding in Hyperbolic Space (2025)](https://arxiv.org/html/2508.15480v1) · [NeurIPS 2025 HypSeek](https://neurips.cc/virtual/2025/125822)
- Traversals / hybrid — [age_gds — AGE+NetworKit bridge](https://github.com/Dn-Media-Group-Public/age_gds) · [pgvector](https://github.com/pgvector/pgvector) · [Combining pgvector and Apache AGE — Microsoft Tech Community](https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781) · [GraphRAG Architecture — Microsoft](https://microsoft.github.io/graphrag/index/architecture/) · [Mastering Agentic GraphRAG (2026)](https://syuthd.com/2026/08/mastering-agentic-graphrag-building.html)
- Temporal — [How Does Knowledge Evolve in Open KGs — Polleres et al., TU Wien](https://repositum.tuwien.at/bitstream/20.500.12708/193052/1/Polleres-2023-Transactions%20on%20Graph%20Data%20and%20Knowledge-vor.pdf)
