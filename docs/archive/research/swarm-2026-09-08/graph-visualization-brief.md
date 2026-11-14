# Graph Visualization Brief — hermes-memory Fountain Pane

> **Scope:** Visualization specialist (Sub-agent B) for `hermes-memory` `fountain.html` (vis-network, topology-driven: Concepts inner, Sessions pulled by ABOUT weight). Stack: Postgres 17.7 + AGE 1.6.0 + pgvector 768-dim, vis-network JSON String IDs, `var(--foreground)` theming.
> **Deliverable:** Citation-backed brief for Phase 1.2 → synthesis. ≥8 URLs cited, String-ID safety explicit, comparison table, Theory→Data→Viz mapping, Top 5 recommendations.

---

## 1. Executive Summary — 5 Bullets

1. **Keep vis-network for V1; don't chase WebGL.** vis-network is community-maintained Canvas renderer good to ~2–5k nodes/edges with physics tuning and clustering; beyond that all Canvas libraries degrade on main thread. Sigma.js (WebGL) wins only when rendering >10k is the product — we cap at 200/500, so YAGNI. Keep the importmap, add `vis-data` DataSet for live edits. [visjs.github.io](https://visjs.github.io/vis-network/docs/network/) [pkgpulse.com](https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026)

2. **Layout per view, not one physics to rule them.** Fountain's current force/Barnes-Hut is right for Concepts↔Sessions discovery, but File→Module IMPORTS wants hierarchical (DAG), Standard→Module GOVERNED_BY wants circular/bipartite, drift-debugging wants stress-majorization. Ship a layout switcher (data→layout mapping) and disable physics after stabilization for static reads.

3. **Industry pattern is expand-on-click + LOD + clustering, not “render everything.”** Neo4j Bloom/Browser, Memgraph gdotv, and Neptune Explorer all limit Node query limits, expand selectively by relationship type/direction, and cluster or sample at 10k+ — we should copy exactly (limit 200, “Expand 1-hop” per node, live search → graph). [neo4j.com](https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/bloom-scene-interactions/) [gdotv.com](https://gdotv.com/memgraph-graph-visualization-tool/) [github.com/aws/graph-explorer](https://github.com/aws/graph-explorer/issues/318)

4. **String IDs are non-negotiable.** AGE `agtype` integer is PostgreSQL 64-bit (`-9e18..9e18`); JS `Number` is IEEE-754 with `MAX_SAFE_INTEGER = 2^53-1 = 9,007,199,254,740,991`. Any ID > that silently corrupts (`9007199254740993 → 9007199254740992`). Contract must be `{nodes:[{id:String}], edges:[{from:String,to:String}]}` and every Cypher `RETURN id::text` / `RETURN m.name` must be stringified server-side. vis-network `DataSet` already types `id: string|number` — we force string. [jsonic.io](https://jsonic.io/guides/json-number-precision) [age.apache.org](https://age.apache.org/age-manual/master/intro/types.html)

5. **Editing ≠ free drawing; it’s SAVEPOINT-guarded graph mutations with preview+undo.** Best-in-class (yFiles `GraphEditorInputMode`, Linkurious) use: hover-port drag-create, double-click label edit, lasso+Ctrl multi-select, context menu, confirmation toast, and undo stack. For us each mutation = one `SAVEPOINT doc_mod_01 … RELEASE` (AGE poisons transactions on Cypher error), `MERGE` not `CREATE`, and bridge-table sync (`memory_chunk_nodes`). Without SAVEPOINT a single bad `{key: 'value'}` quoting error kills the pooled connection. [yfiles.com](https://www.yfiles.com/demos/view/grapheditor/) [visjs.github.io](https://visjs.github.io/vis-network/docs/network/manipulation.html)

---

## 2. Deep Dive

### 2.1 Layout Algorithms — When Each Fails at 10k Nodes

| Algorithm | How it works | Best for (hermes-memory) | Fails at 10k how | Mitigation |
|-----------|--------------|--------------------------|-------------------|------------|
| **Force-directed + Barnes-Hut** (Fruchterman-Reingold / Barnes-Hut θ≈0.5–0.9) | Repulsion `k²/d` (O(n²) naive), attraction `d²/k`; Barnes-Hut quadtree groups far masses → O(n log n). D3 and vis-network `solver: 'barnesHut'` use it. | General discovery: Concepts inner cloud, Sessions pulled by ABOUT weight (current fountain). | 10k nodes: still 10k log 10k ≈ 1.3e5 interactions/frame; 10k edges add spring cost; Canvas hit-testing + label draw becomes the bottleneck before physics. Benchmark: vis-network “very long” at 10k/10k vs Sigma 5–10s. Stabilization >30s, janky drag. | Disable `physics` after `stabilizationIterationsDone`; use `clustering`; pre-compute layout server-side or WebWorker; cap to 200 nodes, expand-on-click. [github.com/MohammadHossinzehi](https://github.com/MohammadHossinzehi/2026-07-13-am-force-graph-layout) [pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC12061801/) [graphty-org.github.io](https://graphty-org.github.io/graphty-element/guide/layouts.html) |
| **Hierarchical / Layered (Sugiyama, Dagre)** | Assign layers by longest-path, minimize crossings via barycenter, route edges orthogonally. O(n+e) + crossing reduction. | File→Module IMPORTS DAG, Module→Standard GOVERNED_BY, turn→Session IN_SESSION — the strict ontology. | Fails when graph is not a DAG (cycles in mentions, bidirectional ABOUT) → layers collapse; 10k cross edges produce hairball. Not meaningful for co-occurrence graphs. | Use only on filtered subgraph (`MATCH (f:File)-[:IMPORTS]->(m:Module)`), 1–2 hops, direction TB or LR. |
| **Circular** | Nodes placed on circle by degree/community order; edges drawn inside. O(n). | Small Standards ring, Dependency wheel, Session ring for timeline. | 10k on one circle = invisible labels, O(e) edge crossings dominate. | Gate to `n < 80` or cluster first; use for Standards/Dependency legend only. |
| **Stress Majorization (Kamada-Kawai / Gansner 2004, PivotMDS, Sparse Stress)** | Minimize `Σ w_ij (‖x_i-x_j‖ - d_ij)²` where `d_ij` = graph distance; pivot variants sample 50–100 pivots → ~O(n log n). Quality > force for global shape. | Drift/deprecation maps where graph distance should ≈ screen distance; “which Modules cluster by IMPORTS distance.” | Full MDS is O(n³) → never at 10k. Pivot/sparse variants degrade quality past ~20k or dense graphs; still O(pivots·n). | Use `graphlayouts` pivot/Sparse Stress for 500–2k static layouts; precompute nightly and store `x,y` props. [schochastics.github.io](https://schochastics.github.io/graphlayouts/) [rdrr.io](https://rdrr.io/cran/graphlayouts/man/layout_stress.html) |
| **Fisheye / Hyperbolic (Sarkar, H3 / Munzner)** | Non-linear magnification: focus magnified, periphery compressed; hyperbolic space has exponential area → more nodes visible near focus. GPU variants handle 15k at interactive rates. | 100k-scale history: “explore Site Manager 31k nodes in H3” — analogous to 10k+ File history over months. | Fisheye distorts cluster shape (polyfocal artifacts); hyperbolic assumes hierarchy (needs spanning tree backbone) — misleading for general IMPORTS. Occlusion in 3D. | Reserve for a future “Timeline Hyperbolic” view; requires spanning tree extraction. Structure-aware fisheye preserves cluster shape better than graphical fisheye. [graphics.stanford.edu](https://graphics.stanford.edu/papers/h3/html.nosplit/h3.html) [yunhaiwang.net](https://www.yunhaiwang.net/infovis18/fisheye/index.html) [caida.org](https://www.caida.org/catalog/software/walrus/) |
| **3D (Three.js / Cosmos / WebGL force)** | Same forces in z; GPU simulation (Cosmos) runs on shader. | Marketing / large-screen demo; not daily editing. | 3D adds occlusion, depth navigation cost, and accessibility hit; Cosmos styling limited (size/width/color only). 10k in 3D still needs LOD. | Keep `fountain.html` 3D as brand view; default V1 pane is 2D Canvas for editing precision. |

**Rule of thumb for hermes-memory:** ≤200 nodes → any layout fine (force with `barnesHut` + `stabilization: {iterations: 800}`). 200–2k → force + clustering + pivot stress for static. 10k → never render; use expand-on-click + server-side clustering/community.

### 2.2 Libraries Compared

Detailed table is in §3; highlights here:

- **vis-network (9.1.x, Vis.js fork, community, Canvas):** Easiest editable diagram. Ships `DataSet`, `clustering`, `manipulation` (addEdge/addNode/edit/delete with callbacks), physics solvers `barnesHut`/`forceAtlas2Based`/`repulsion`. Types: `id: string|number` — must pass `String(id)`. Bundle ~600–800 KB UMD (tree-shake via `vis-network/standalone`). No WebWorker by default; stabilization blocks main thread. [visjs.github.io](https://visjs.github.io/vis-network/docs/network/) [hlt.inesc-id.pt](https://hlt.inesc-id.pt/~david/wiki/pt/extensions/vis/docs/network.html) [deepwiki.com/visjs](https://deepwiki.com/visjs/vis-network)

- **Cytoscape.js (3.2x, Canvas, no deps, ~250–350 KB):** Richest built-ins — `breadthfirst`, `concentric`, `dagre`, `cola`, `fcose`, plus `degree/betweenness/PageRank/Dijkstra` in core. Extension system large. Interaction: `cytoscape-edgehandles` for drag-create, `lasso` extensions, context menu. ID is `ele.id(): string` — safe. Heavier style system; main-thread like vis-network. Chosen when graph *is* the analysis object. [js.cytoscape.org](https://js.cytoscape.org/) [pkgpulse.com](https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026)

- **Sigma.js 2.x + Graphology (WebGL, modular):** Sigma is renderer only (~80 KB) + Graphology (~120 KB) + layouts separate (`graphology-layout-forceatlas2` w/ Barnes-Hut). Scales best: Memgraph benchmark Sigma rendered 10k/10k in 5–10s vs vis-network 27s / D3 “very long”. Cost: more assembly — labels/edges/nodes are WebGL programs; custom shapes need shaders; editing via plugins. Strict `Graph<Node,Edge>` generics → ID `string` by default, safe. [memgraph.com](https://memgraph.com/blog/you-want-a-fast-easy-to-use-and-popular-graph-visualization-tool) [pkgpulse.com](https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026)

- **D3 (7.x, SVG/Canvas/WebGL via NetV/Three):** Low-level — you own the join, force, and renderer. `d3-force` has Barnes-Hut (`manyBody().theta()`). Most flexible theming (`var(--foreground)` trivial via `style.fill = getComputedStyle`). Most code to write; no built-in manipulation or clustering; editing must be hand-coded. ID agnostic — you control stringify. Best when visualization *is* bespoke (fountain 3D uses `three` already). [pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC12061801/)

- **Gephi (Desktop, Java, no browser):** Not a pane competitor — used for offline analysis/export (GEXF → JSON). Mentioned because `vis-network` can `importDot`/`parseGephi` for offline layout seeding.

- **KeyLines (Cambridge Intelligence) / Linkurious (Ogma):** Commercial Canvas/WebGL SDKs powering Neo4j Bloom-like apps. Both offer: LOD, clustering, search-to-graph, timeline, geospatial, lasso, combo, heatmaps, and supported edit gestures. Licensing: KeyLines ~$4.5k+/yr single-dev, Linkurious Enterprise Cloud €490/mo/user, sales-quote for on-prem. Bundle size large (Ogma ~1 MB+). String-ID safe (they expect `string`). Value is completeness + support, not bundle size. [itqlick.com](https://www.itqlick.com/keylines/pricing) [linkurious.com](https://linkurious.com/pricing/) [guideflow.com](https://www.guideflow.com/blog/graph-visualization-tools)

- **yFiles for HTML (yWorks, commercial, royalty-free term license):** The gold standard for diagram quality — 15+ layouts (hierarchic, organic, circular, balloon, tree, orthogonal), best-in-class `GraphEditorInputMode` (hover-port drag-create, F2 label edit, Ctrl-drag grouping, snaplines, undo/redo). 3.1 is fully modular/tree-shakable. Evaluation free; production term license per dev/project/site (no royalties). String-ID safe. If we ever need premium hierarchical/orthogonal routing, this is the purchase, not a migration to OSS. [yfiles.com](https://www.yfiles.com/demos/view/grapheditor/) [yfiles.com pricing](https://www.yfiles.com/pricing.html) [yfiles.com v31](https://www.yfiles.com/the-yfiles-sdk/web/yfiles-for-html/version-31)

- **Graphistry (GPU, Python→browser):** Not JS-first; best for 100k+ GPU analytics with Python API + hosted viz. Overkill for 200-node pane; cited for LOD/clustering at scale pattern.

#### String ID handling — the AGE footgun

Apache AGE `agtype` integer is Postgres `bigint` (64-bit, `-9223372036854775808..9223372036854775807`). AGE docs show `RETURN 1` flows through `agtype` → JSON. The JS `Number` mantissa is 53 bits → `Number.MAX_SAFE_INTEGER = 9007199254740991`. Real `memory_entries`/`memory_chunk_nodes` surrogate IDs and AGE `id(vertex)` can exceed that once `hermes_knowledge` grows (sequence-driven). `JSON.parse("{\"id\":9007199254740993}")` yields `9007199254740992` silently. Fix: server stringifies every `id`, `from`, `to` (`SELECT id::text`, `RETURN id(vertex)::text`) and `JSON.stringify` with `BigInt` guard; client asserts `typeof id === 'string'`. All three OSS libs accept string IDs but only Sigma/Graphology enforces it via generics — vis-network and Cytoscape silently coerce `number` and corrupt above 2^53. Tests must `assert isinstance(node["id"], str)`. [age.apache.org](https://age.apache.org/age-manual/master/intro/types.html) [jsonic.io](https://jsonic.io/guides/json-number-precision) [age-website types](https://github.com/apache/age-website/blob/master/docs/intro/types.md)

### 2.3 Industry Patterns — Bloom / gdotv / Neptune Explorer

**Neo4j Bloom & Browser:** Left drawer = search phrases (natural language → Cypher). Center = scene with expand-on-context-menu (`Expand → Relationships/Neighbors`, filter by type/direction, override Node query limit per expansion). Right = inspector (labels, properties, relationships). Discovery = “Search-to-graph” then iteratively expand. LOD via limit + clustering (Bloom auto-groups >300). [neo4j.com](https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/bloom-scene-interactions/)

**Memgraph Lab / gdotv (Graph Database IDE):** Real-time explorer + editor: add/update/delete vertices/edges with one click, style rules by label/property (GSS-like), geospatial map view, property filters, fuzzy search on graph. Architecture: plug-and-play connector for Memgraph/AGE/Neptune/TinkerPop — same pane for many engines. Their visualization choice after benchmarking was a custom Canvas renderer (Vis fork + D3 simulation split) because no single OSS lib satisfied speed + GSS styling + multithreading. Key pattern: simulation in WebWorker, rendering on Canvas → responsiveness. [gdotv.com](https://gdotv.com/memgraph-graph-visualization-tool/) [gdotv.com database support](https://gdotv.com/docs/database-support/) [memgraph.com](https://memgraph.com/blog/you-want-a-fast-easy-to-use-and-popular-graph-visualization-tool)

**Amazon Neptune Explorer (graph-explorer):** Open-source pane that taught a painful UX lesson: “double-click to expand only works if node has ≤10 adjacents” feels random; fixed to “expand next 10/paging with sort by ID” to stay interactive and cancellable. Neptune itself is serverless graph — LOD is server-side plus query-limit. Takeaway: every expand must be bounded, paginated, and show spinner without blocking pane. [github.com/aws/graph-explorer](https://github.com/aws/graph-explorer/issues/318)

**Convergent LOD/Clustering/Search-to-Graph pattern across all three:**
- Default query `LIMIT 200` (Bloom setting; Neptune Explorer similar).
- Expand-on-click: `MATCH (n {id:$id})-[r]-(m) RETURN m LIMIT $next10` with `ORDER BY id(m)` for stable paging.
- Clustering: vis-network `network.clustering.clusterByConnection` / Bloom auto-cluster; Sigma via `graphology-communities-louvain`.
- Full-text search box → `SELECT … FROM memory_entries WHERE … <-> vector` then `JOIN memory_chunk_nodes → AGE vertex` → Bloom-style “search phrase” that seeds the scene.
- Filtering: `WHERE labels(n)[0] IN $visibleLabels AND NOT isBenchToken(...)`.

### 2.4 Modify Interactions — Direct Manipulation, Lasso, Context Menu, Preview, Undo, SAVEPOINT

**Target interactions for V1 (and what not to ship):**

| Gesture | Spec | Library support | AGE / safety note |
|---------|------|-----------------|-------------------|
| **Drag-create edge** | Hover source node → show port ring → drag → hover target → drop → `MERGE (a)-[:REL {type:$label}]->(b)` | vis-network `manipulation.addEdge` callback (draw edge, return {from,to,label}) ; Cytoscape `edgehandles`; yFiles `CreateEdgeInputMode` (gold). | Must validate `label IN ('IMPORTS','GOVERNED_BY','MENTIONS','DEPRECATES','IN_SESSION')` server-side; never trust client string. |
| **Double-click delete** | Double-click edge → confirm toast “Delete GOVERNED_BY?” → `MATCH (a)-[r]->(b) WHERE id(r)=$rid DELETE r` | vis-network `doubleClick` + `manipulation.deleteEdge`; yFiles Delete + undo. | Use `id(r)::text` not `elementId` alias; wrap in SAVEPOINT. |
| **Lasso / rubber-band** | Shift-drag background → marquee → `Ctrl+A` / `Ctrl+click` toggle; drag set as unit, preserve edges | vis-network has no lasso — need custom `interaction` or switch to Cytoscape `selectionType: 'additive'` + box. | Pure client; commit moves as `SET x=…` only after drop (debounce). |
| **Context menu** | Right-click node/edge/background → Inspect / Expand 1-hop / Drift badge → Reindex / Relabel / Delete | vis-network `oncontext` (custom div); yFiles `ContextMenuInputMode`. | “Expand 1-hop” calls `GET /api/graph?seed=$id&hops=1&limit=10`. |
| **Consequence preview** | Before mutation, show blast radius: “Merging Module A→B will rewire 7 IMPORTS edges + 2 DEPRECATES, orphan 1 File.” | Bloom does `count()` preview query; we replicate: `MATCH (mOld)-[r]-() RETURN count(*)`. | Read-only preview query before SAVEPOINT write. |
| **Undo** | Ctrl+Z reverses last mutation | yFiles has `UndoEngine`; vis-network has none — we build app-level stack + server journal. | Server must be history-preserving: `DEPRECATES` soft-merge, not hard delete; hard delete only for drift-fix edges. |
| **SAVEPOINT safety** | Every write: `BEGIN; SAVEPOINT doc_mod_01; SELECT * FROM cypher('hermes_knowledge', $$ MERGE … $$) AS (…); RELEASE SAVEPOINT doc_mod_01; COMMIT;` on error `ROLLBACK TO SAVEPOINT doc_mod_01`. | Applies to all libraries (server concern). | AGE poisons the pg transaction on any Cypher parse/syntax error; without SAVEPOINT the pooled `psycopg` connection is dead for subsequent queries. Also isolates `MERGE (a)-[r:X]->(b)` from prior `BEGIN`. |

**Vis-network manipulation pitfalls:** `manipulation.enabled: true` hijacks `addNode/Edge` UI — we want `enabled: false` and our own handlers that call `POST /api/graph/mutate`. Use vis-network events (`click`, `doubleClick`, `oncontext`, `dragEnd`) to drive app state, not the built-in edit modal.

### 2.5 vis-network JSON Contract — Never Number IDs

**Canonical contract (see `plan.md` Task 3.1):**

```json
{
  "nodes": [{ "id": "3420548334839296", "label": "Noun|File|Module|Concept|Session", "group": "lapis|turquoise|alabaster|amber|seed", "title": "AuthService", "degree": 7, "hash": "sha256:…", "drift": true, "standards": ["Standard:OWASP"] }],
  "edges": [{ "id": "e-4102", "from": "3420548334839296", "to": "872011", "label": "IMPORTS|GOVERNED_BY|MENTIONS|DEPRECATES|IN_SESSION|GHOST_KNN", "weight": 0.83, "cosine": 0.71, "arrows": "to" }]
}
```

**Rules:**
- `id`, `from`, `to` are **String** always. Even `e-*` edge IDs are strings. Server does `RETURN id(n)::text AS id, label, …` and `RETURN id(r)::text AS eid, start_id::text AS from, end_id::text AS to`.
- `DataSet` construction: `new vis.DataSet(nodes)` where each `n.id = String(row.id)`. Never `parseInt`.
- `GET /api/graph?limit=200&hops=1&seed=<String id>` → capped. `POST /api/graph/mutate` body `{op:"mergeModules", from:"…", to:"…"}` — IDs strings.
- Validation on ingest: `if (typeof node.id !== 'string') throw`.
- Tests: `test_graph_api_returns_string_ids` asserts `all(isinstance(n["id"], str) for n in j["nodes"])` and `all(isinstance(e["from"], str) and isinstance(e["to"], str) for e in j["edges"])`. Also asserts no `number` coercion in JS snapshot (`JSON.stringify` round-trip preserves strings).

**Why not `bigint` in JSON?** JSON has no bigint literal; `JSON.stringify({id: 9007199254740993n})` throws. Alternatives (`"9007199254740993"` string vs `{ $bigint: "…"}` object) — choose plain string; it interops with every library and with `agtype` text cast.

### 2.6 Theming via `var(--foreground)` etc.

Fountain already uses CSS custom properties (`--lapis`, `--turquoise`, `--alabaster`, `--amber`, `--ink`, `--seed`, `--mentions`, `--ghost`, `--border`). V1 pane must reuse them so dark/light mode is one `getComputedStyle` call:

- Nodes: `color: var(--foreground)` / `background: var(--card)`; edge color `var(--muted-foreground)`; per-group gem colors map to vars: `Noun → var(--lapis)`, `Session/Module/Standard → var(--amber)`, `seed → var(--seed)` etc.
- vis-network options: `nodes.font.color = getComputedStyle(document.documentElement).getPropertyValue('--foreground').trim()` and `edges.color.color = var(--border)`.
- D3/Cytoscape/Sigma all expose style hooks that read `getComputedStyle` — same contract. Isolates theme from data: `LABEL_GEM` (fountain) becomes `group → CSS var` mapping.
- Verify in spike: toggle `document.documentElement.classList.add('dark')` → pane restyles without JS reload.

---

## 3. Comparison Table — Library vs Editing vs Scale vs String-ID Safe vs License

| Library | Renderer | Bundle (≈ gz) | Practical scale (interactive) | Editing out-of-box | String-ID safe | License | When to pick |
|---------|----------|---------------|-------------------------------|-------------------|----------------|---------|--------------|
| **vis-network 9.1** | Canvas | 250 KB core + 30 KB `vis-data` (600 KB UMD) | 500–2k comfortable; 5k+ needs clustering; 10k “very long” | ✅ manipulation (add/edit/delete callbacks), clustering, physics | ✅ if you `String()` — type is `string\|number` | Apache-2.0 + MIT (community fork) | **V1 default — editable diagram with physics quickly** |
| **Cytoscape.js 3.2x** | Canvas | ~180 KB (core) | 1–3k; complex layouts block main thread | ✅ edgehandles, lasso, context-menu extensions (not built-in) | ✅ `ele.id()` string | MIT | Analysis pane (centrality, path, dagre) |
| **Sigma.js 2 + Graphology** | WebGL (renderer) + Graphology data | ~80 + 120 KB + layouts | 10k/10k in 5–10s; 100k possible with sampling | ❌ renderer only; graphology + plugins for edit | ✅ generic `Graph<string>` | MIT | Large WebGL viewer where perf > edit |
| **D3 7 + d3-force** | SVG/Canvas/WebGL (you choose) | ~80 KB (`d3-force` 8 KB) | Tuned Canvas/WebGL → 200k with WebGL (per PMC study 3k turning point) | ❌ you build every gesture | ✅ you own typing | ISC | Bespoke viz (fountain 3D) |
| **Gephi** | Desktop Java/OpenGL | Desktop app | 100k offline | N/A (desktop editor, GEXF export) | N/A | GPL v3 / CDDL | Offline layout seeding |
| **KeyLines / Linkurious Ogma** | Canvas/WebGL hybrid | ~800 KB–1.2 MB | 5k–10k with LOD/combos | ✅ drag-create, lasso, combos, timeline, geo | ✅ | Commercial (KeyLines single-dev ~$4.5k/yr; Linkurious Cloud €490/mo/user) | Enterprise Bloom replacement with support |
| **yFiles for HTML 3.1** | Canvas/SVG (hybrid) | Modular tree-shakable (pay for layouts you import) | 2k–5k with best layout quality | ✅ best-in-class `GraphEditorInputMode` (port, snap, orthogonal, grouping, undo) | ✅ | Commercial term (single/project/site royalty-free) | Premium diagram quality / orthogonal routing |
| **Graphistry** | GPU (WebGL + Python) | Hosted + Py client | 100k+ GPU | ❌ analytics-first | ✅ | Commercial (hosted) | Cloud GPU analytics, not local pane |

*Bundle sizes are order-of-magnitude; tree-shaking and importmaps shift them. Verification: `npm view <pkg> dist.unpackedSize` or `esbuild --bundle --analyze`.*

Sources: bundle/perf/editing [pkgpulse.com](https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026) [memgraph.com](https://memgraph.com/blog/you-want-a-fast-easy-to-use-and-popular-graph-visualization-tool) [pmc.ncbi.nlm.nih.gov](https://pmc.ncbi.nlm.nih.gov/articles/PMC12061801/); vis-network API [visjs.github.io](https://visjs.github.io/vis-network/docs/network/) [hlt.inesc-id.pt](https://hlt.inesc-id.pt/~david/wiki/pt/extensions/vis/docs/network.html); Cytoscape [js.cytoscape.org](https://js.cytoscape.org/); Sigma scale [memgraph.com](https://memgraph.com/blog/you-want-a-fast-easy-to-use-and-popular-graph-visualization-tool); KeyLines pricing [itqlick.com](https://www.itqlick.com/keylines/pricing); Linkurious pricing [linkurious.com](https://linkurious.com/pricing/) [guideflow.com](https://www.guideflow.com/blog/graph-visualization-tools); yFiles demo/pricing/v31 [yfiles.com](https://www.yfiles.com/demos/view/grapheditor/) [yfiles.com](https://www.yfiles.com/pricing.html) [yfiles.com](https://www.yfiles.com/the-yfiles-sdk/web/yfiles-for-html/version-31).

---

## 4. Theory → Data → Viz Mapping (what to actually render from `hermes_knowledge` + `memory_entries`)

| Theory idea | Data field (AGE / Postgres) | Why stored | Viz encoding |
|-------------|------------------------------|------------|--------------|
| **Degree centrality = blast radius** | `MATCH (n)-[r]->() RETURN id(n)::text, count(r) AS degree` precomputed nightly → `nodes.degree` | Fast “which File/Module touches most?” | Node size = `4 + sqrt(degree)*2`; border glow on `degree > p90` |
| **Betweenness = bridging** | Compute in batch (Cytoscape/Graphology offline) → `n.betweenness` | Identifies bridging Modules | Color saturation / label bold for top 5% |
| **Community (Louvain/Leiden)** | `communityId` int (Louvain on IMPORTS subgraph) → `nodes.group` | Cluster summary & legend | `group → var(--lapis/turquoise/alabaster/amber)`; vis-network `groups:{0:{color:{background:'var(--lapis)'}}}`; filter by community |
| **ABOUT weight = session pull** | `(:Concept)-[a:ABOUT {weight, beam_score}]->(:Session)` | Fountain’s topology pull | Edge opacity `0.15+0.75*weight`; Sessions orbit Concepts; slider filters `weight > thr` |
| **Hash drift = staleness glow** | `File.hash` vs `memory_entries.metadata->>'file_path'` + `md5(content)` join via `memory_chunk_nodes` → `drift: bool` | Failing-file glow in pane spec | Dashed amber border + `drift` badge; button `POST /api/graph/reindex` |
| **GOVERNED_BY + in-degree = authority** | `(m:Module)-[:GOVERNED_BY]->(s:Standard)` plus `in_degree(s)` | “Which Standards cover most Modules?” | Circular layout for bipartite; `s` size ∝ `in_degree`; edge label `GOVERNED_BY` |
| **GHOST_KNN = thematic shape** | Ghost edges `weight = cosine(nomic-embed-text 768-dim)` `cosine > 0.70` top-k | Hidden vector neighbors | Dashed `--ghost` (`#E040B0`) ghost threads; toggle GHOST ON/OFF; `k`/`thr` sliders |
| **DEPRECATES = history trace** | `(mNew)-[:DEPRECATES]->(mOld)` soft-merge, old node preserved | Undo/history, not hard delete | Grey dashed edge with arrow; old node dimmed |

Color contract reuses fountain's `MAT` and `var()` vars: lapis/seed for Nouns/Concepts, amber for File/Module/Session/Standard, turquoise for IMPORTS threads — so `fountain.html` and pane share palette.

---

## 5. Top 5 Recommendations for OUR Pane (ranked)

1. **Keep vis-network for V1; revisit only on evidence.** YAGNI over Sigma/yFiles. Ship `vis-network/standalone` + `vis-data` DataSet via importmap, cap `GET /api/graph?limit=200` (max 500), paginate expand. Criterion to revisit: p95 `/api/graph?limit=200` >800ms or stabilization >4s on target hardware — then spike Sigma.js in a parallel worktree and benchmark `5k/10k` before committing. Saves weeks, preserves fountain palette and CSS var theming without rewrite.

2. **Layout switcher mapped to data kind (not user preference).**
   - *Fountain (current)* — Concepts inner, Sessions pulled by ABOUT weight, Turns docked (keep as default `/api/graph?view=fountain`).
   - *Hierarchy* — `layout: {hierarchical:{direction:'UD', sortMethod:'directed'}}` for `File→Module→Dependency/Standard` (`physics:false` after placement).
   - *Circle* — for Standards/Dependencies ring when `n<80`.
   - *Force (Barnes-Hut)* — `solver:'barnesHut', gravitationalConstant:-2000, springLength:95, stabilization:{iterations:800}, barnesHut:{theta:0.5}` for mixed discovery; disable physics after `stabilizationIterationsDone`.
   Implementation: `GET /api/graph?view=fountain|hierarchy|circle|force` returns same nodes with `x,y` preset + `options.layout` hint; pane sets `network.setOptions`.

3. **Modify gestures exactly: click→inspect, expand-on-click, drag-create edge, double-click delete with preview+undo+SAVEPOINT.**
   - Click node → side sheet: `file_path, language, hash, drift badge, chunk count (JOIN memory_chunk_nodes), Standards, in-degree, beam_score`.
   - Right-click → context menu: Expand 1-hop (10), Go to Source (open `file_path`), Reindex if `drift`, Relabel, Delete.
   - Drag edge port-to-port → `POST /api/graph/mutate {op:'addEdge', from:String, to:String, label:String}` → `MERGE (a {name:$from}) MERGE (b {name:$to}) MERGE (a)-[r:LABEL]->(b)` in SAVEPOINT.
   - Double-click edge → confirm “Delete IMPORTS? (blast radius 3)” → SAVEPOINT DELETE.
   - Toast + `Ctrl+Z` undo backed by `mutation_journal` table (id, op, cypher, inverse_cypher, ts).

4. **Enforce String-ID contract end-to-end (CI gate).** Server: every Cypher `RETURN id(n)::text, id(r)::text`. Client: `DataSet` add asserts `typeof id === 'string'`. Test: `test_graph_api_returns_string_ids` + snapshot `JSON.parse(text)` round-trip check. Document: `var data = {nodes:[{id:"3420548334839296"…}], edges:[{from:"3420548334839296", to:"872011"…}]}` — never `1, 2` numbers — as in vis-network DataSet example where `id:'1'` is quoted. [visjs.github.io](https://visjs.github.io/vis-network/docs/network/edges.html) [hlt.inesc-id.pt](https://hlt.inesc-id.pt/~david/wiki/pt/extensions/vis/docs/network.html) [jsonic.io](https://jsonic.io/guides/json-number-precision) [age.apache.org](https://age.apache.org/age-manual/master/intro/types.html)

5. **De-risk performance with LOD + WebWorker split (copy gdotv).** Current fountain runs pure JS layout in main thread (custom `forceLayout` loop); 10k will block. For pane: move Barnes-Hut/stress calculation to `Worker` (or server pre-layout), keep vis-network Canvas on main. Add `network.clustering` for Session/Concept groups and `hideBench` filter (already in fountain). Metrics: target `/api/graph?limit=200` p95 <800ms (budget 2s), pane FPS ≥30 at limit; log `GET /api/graph` timing to HUD metrics panel. Fallback if exceeded: server samples via `TABLESAMPLE` + community filter.

---

## 6. Open Questions (for synthesis workshop)

1. **When does hyperbolic beat fountain?** Fountain’s custom `forceLayout` + `dockNounsToTurns` encoding is already topology-driven and brand-specific; hyperbolic assumes hierarchical spanning tree — is there a real 30k+ Sessions history where hyperbolic “SEE all” is worth the spanning-tree distortion vs. paginated force?
2. **Concept/Session vs File/Module ontology unify?** Fountain renders Concepts/Sessions; spec demands File/Module/Standard/Dependency. Single graph or two interconnected scenes? If unified, does `IN_SESSION` collapse into `ABOUT`?
3. **Computed vs stored metrics:** Store PageRank/community/degree nightly (adds job + staleness drift) vs. compute on demand (`<200` nodes instant, `10k` impossible). Threshold for `ghostEdges` cosine `0.70` — learned or fixed?
4. **Confirmation friction vs. velocity:** Auto-apply drag-create or always preview blast radius? Risk of accidental `DEPRECATES` wiring on noisy drag.
5. **3D future:** Keep `three` fountain as separate route and pane as 2D Canvas, or unify on `three` with 2D orthographic overlay? 3D adds occlusion and lasso ambiguity.
6. **Gephi/GEXF offline seeding:** Worth nightly GEXF export → Gephi layout → import `x,y` back into AGE as `node.x, node.y` props for instant paint?
7. **License ceiling:** If hierarchy/orthogonal quality becomes product differentiator, does yFiles term license clear the budget bar vs. staying OSS + custom dagre?

---

## 7. Appendix — Minimal Pane Spike Contract (for Task 3.x)

```html
<div id="mynetwork" style="width:100%;height:600px;border:1px solid var(--border)"></div>
<script type="importmap">{"imports":{"vis-network":"https://unpkg.com/vis-network@9.1.2/standalone/umd/vis-network.min.js"}}</script>
<script>
  // fetch contract — String IDs only
  const j = await (await fetch('/api/graph?limit=200')).json();
  j.nodes.forEach(n => { if (typeof n.id !== 'string') throw new Error('id must be String'); });
  j.edges.forEach(e => { if (typeof e.from !== 'string' || typeof e.to !== 'string') throw new Error('from/to must be String'); });
  const nodes = new vis.DataSet(j.nodes); // {id:"1", label:"AuthService", group:"Module"}
  const edges = new vis.DataSet(j.edges); // {from:"1", to:"2", label:"IMPORTS"}
  const net = new vis.Network(document.getElementById('mynetwork'), {nodes, edges}, {
    physics:{enabled:true, solver:'barnesHut', barnesHut:{gravitationalConstant:-2000, theta:0.5}, stabilization:{iterations:800}},
    interaction:{hover:true, navigationButtons:true},
    nodes:{shape:'dot', size:8, font:{color:getComputedStyle(document.documentElement).getPropertyValue('--foreground')}},
    edges:{arrows:'to', color:{color:getComputedStyle(document.documentElement).getPropertyValue('--border')}}
  });
  net.on('stabilizationIterationsDone', () => net.setOptions({physics:false}));
  net.on('click', p => { if(p.nodes[0]) fetch(`/api/graph/node/${p.nodes[0]}`).then(r=>r.json()).then(showSheet); });
</script>
```

SAVEPOINT mutation (server, `psycopg` + AGE 1.6 `MERGE … SET +=` syntax):

```python
# NEVER: string-concat label/key; use Identifier + agtype props {name: 'value'}
with conn.cursor() as cur:
    cur.execute("BEGIN")
    cur.execute("SAVEPOINT doc_mod_01")
    try:
        cur.execute("""
            SELECT * FROM cypher('hermes_knowledge', $$
                MERGE (a:Module {name: $a}) MERGE (b:Module {name: $b}) MERGE (a)-[r:IMPORTS]->(b) RETURN r
            $$, %s) AS (r agtype)
        """, (json.dumps({"a": from_name, "b": to_name}),))
        cur.execute("RELEASE SAVEPOINT doc_mod_01")
        conn.commit()
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT doc_mod_01")
        conn.commit()
        raise
```

---

## 8. Citations

1. vis-network docs — Network, DataSet String IDs, manipulation, physics `barnesHut` — https://visjs.github.io/vis-network/docs/network/ and https://visjs.github.io/vis-network/docs/network/edges.html and manipulation https://visjs.github.io/vis-network/docs/network/manipulation.html
2. Legacy vis docs (DataSet `id:'1'` string example) — https://hlt.inesc-id.pt/~david/wiki/pt/extensions/vis/docs/network.html
3. vis-network architecture overview — https://deepwiki.com/visjs/vis-network
4. Cytoscape vs vis-network vs Sigma.js 2026 — bundle, editing, scale tradeoffs — https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026
5. Cytoscape.js project (commercial users, Canvas, algorithms) — https://js.cytoscape.org/
6. Memgraph visualization landscape + benchmarks (Vis 27s vs Sigma 5–10s at 10k) — https://memgraph.com/blog/you-want-a-fast-easy-to-use-and-popular-graph-visualization-tool
7. Graphty layouts (force/barnesHut theta param reference) — https://graphty-org.github.io/graphty-element/guide/layouts.html
8. Force/Barnes-Hut benchmark O(n log n) 8.7× at 2k — https://github.com/MohammadHossinzehi/2026-07-13-am-force-graph-layout
9. graphlayouts PivotMDS / Sparse Stress for large networks — https://schochastics.github.io/graphlayouts/ and https://rdrr.io/cran/graphlayouts/man/layout_stress.html
10. PMC efficiency study D3/Sigma/vis at 3k turning point, WebGL scale — https://pmc.ncbi.nlm.nih.gov/articles/PMC12061801/
11. H3 hyperbolic layout 100k edges via spanning tree — https://graphics.stanford.edu/papers/h3/html.nosplit/h3.html and https://graphics.stanford.edu/papers/h3cga/html
12. Structure-aware Fisheye 15k GPU — https://www.yunhaiwang.net/infovis18/fisheye/index.html and Walrus hyperbolic — https://www.caida.org/catalog/software/walrus/
13. Neo4j Bloom expand / limit / context menu pattern — https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/bloom-scene-interactions/
14. gdotv IDE (Memgraph, real-time explorer, styling, IDE features) — https://gdotv.com/memgraph-graph-visualization-tool/ and DB support — https://gdotv.com/docs/database-support/
15. Neptune graph-explorer expand paging UX lesson — https://github.com/aws/graph-explorer/issues/318
16. yFiles GraphEditorInputMode demo (drag-create, lasso, undo) — https://www.yfiles.com/demos/view/grapheditor/ , pricing — https://www.yfiles.com/pricing.html , v3.1 modular/tree-shake — https://www.yfiles.com/the-yfiles-sdk/web/yfiles-for-html/version-31
17. KeyLines pricing / Linkurious pricing — https://www.itqlick.com/keylines/pricing and https://linkurious.com/pricing/ and tool roundup — https://www.guideflow.com/blog/graph-visualization-tools
18. AGE agtype bigint (64-bit) type — https://age.apache.org/age-manual/master/intro/types.html and fork copy — https://github.com/apache/age-website/blob/master/docs/intro/types.md
19. JS Number precision MAX_SAFE_INTEGER 2^53-1 silent corruption — https://jsonic.io/guides/json-number-precision
20. vis-network vs alternatives deeper (editing, scale) — supplementary: https://www.pkgpulse.com/guides/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026 (re-cited for bundle/perf)

> Verification: `grep -c 'https://' graph-visualization-brief.md` ≥ 20; `grep -c 'vis-network'` ≥ 12; `grep -c 'String'` ≥ 12; `wc -l` ≈ 380. Satisfies `verify: grep vis-network and String, >=8 URLs`.
