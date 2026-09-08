> ⚠️ **2026-09-08 Peer review — Path B first, A later: This spec is codebase-ingest scoped (`:File/:Module/:Dependency/:Imports`) — not the conversational-memory graph (`noun`/`semantic_edge`/`beam_score` → `OC-MVP-3/4` + `PANE_INTERACTION_SPEC.md`). Do NOT run Phase 3 as written.** Flow A/GOVERNED_BY, DEPRECATES merge, Standard prefilter, and `File.beamScore` rest on DDL-only labels with zero writers (see `V1__vector_age.sql:57,69,70` vs `ingest.py:9`) and conflated scoring (see `walk.py:76-91`). Mutations stay **501** per `RELEASE_CRITERIA.md OC-PROD-2`. Verified survivor: String-ID for bigint (+ HNSW > IVFFlat, MERGE-then-SET/SAVEPOINT). Path B (conversational graph) is next; Path A (ingest revival) deferred.

---

# Visualize+Modify Pane Spec

*Swarm 2026-09-08 — Contract for implementer (zero context). Implements synthesis-matrix §4-6. Thinks in vis-network JSON, AGE SAVEPOINT, bridge sync.*

---

## Visual Contract

**Endpoint:** `GET /api/graph?seed=...&hops=1|2&limit=200&standard=...&includeStandards=true`

**Query semantics:**
- `seed` (optional): if present → pgvector ANN `SELECT chunk_id ORDER BY embedding <=> $embedding LIMIT 20` → bridge → Cypher hop; if absent → top-degree nodes `MATCH (f:File)-[r]-() RETURN f ORDER BY count(r) DESC LIMIT $limit`
- `hops` 1|2 (default 1): `MATCH (seed)-[r:IMPORTS|GOVERNED_BY|DEPRECATES*1..hops]->(m)`
- `standard` (optional): prefilter `MATCH (s:Standard {name:$standard})<-[:GOVERNED_BY]-(f) WITH collect(f) AS allowed` before ANN (recall loss avoidance — brief C §2)
- `limit` 20-500 (default 200): server caps at 500

**Response (MANDATORY — String IDs):**
```json
{
  "nodes": [
    {"id":"10133099161583617","label":"AuthService","group":"Module","path":"src/auth/service.ts","degree":12,"pageRank":0.031,"communityId":"3","hash":"abc...","drift":false,"standards":["JWT_Compliance"],"chunkCount":4}
  ],
  "edges": [
    {"from":"10133099161583617","to":"10133099161583618","label":"IMPORTS","weight":1},
    {"from":"10133099161583617","to":"8842012345","label":"GOVERNED_BY","weight":1},
    {"from":"10133099161583619","to":"10133099161583617","label":"DEPRECATES","weight":1}
  ]
}
```
- `id`, `from`, `to` **MUST be String** — `id::text` in SQL or `str(vertex_id)` in Python; never JS Number (loses precision at 2^53, AGE ids are bigint). Test `typeof id === 'string'` fails build if violated.
- Empty graph → `{nodes:[], edges:[]}` (not 404).

**Error shape:** `{error: "message", code:"AGE_TIMEOUT"}` 5xx on Cypher poisoning — but poisoning must be prevented by SAVEPOINT.

---

## Layouts

- **Default (File/Module view):** Force Barnes-Hut `solver: 'barnesHut', gravitationalConstant:-8000, centralGravity:0.3, springLength:95` — O(n log n) (brief B §2.1). Keep fountain topology *only* for concept graph (Concepts inner).
- **Toggle:** `Hierarchical` (`direction:'UD', sortMethod:'directed'`) for `Modules→Files→Dependencies` DAG; `Circular` optional.
- **LOD:** `limit 200` default; beyond 200 client clusters by `communityId` (color) and shows cluster label not individual nodes until expand.

---

## Inspect (click node)

Side sheet (`#inspect`) on `network.on('click', params)`:

- **File:** `path`, `language`, `hash` (short 8), `drift` badge (amber ring if true), `chunkCount` = `SELECT count(*) FROM memory_chunk_nodes WHERE vertex_id::text=$id`, `standards` = `MATCH (f {path:$path})-[:GOVERNED_BY]->(s) RETURN s.name`, `inDegree` = `MATCH (f)<-[:IMPORTS]-() RETURN count(*)`, `pageRank` if exists
- **Module/Dependency/Standard:** `name`, `path` or `kind`, incoming `IMPORTS` count, `GOVERNED_BY` list, `DEPRECATES` chain
- Every claim in sheet is `file_path`-cited via API (`path` field) — no uncited architectural claim.

**Expand:** Button `Expand +1 hop` → `GET /api/graph?seed=<nodeId>&hops=1&limit=50` → `nodes.add/update`, `edges.add` (vis DataSet).

---

## Modify (one mutation to start — safety first)

**Allowed V1 mutations (exactly 3):**
1. Delete edge: `POST /api/graph/edge/delete {fromId:String, toId:String, label:String}` → `MATCH (a)-[r:LABEL]->(b) WHERE id(a)::text=$from AND id(b)::text=$to DELETE r`
2. Merge Modules: `POST /api/graph/merge {fromName, toName}` → `MERGE (new) MERGE (old) MERGE (new)-[:DEPRECATES]->(old)` + move `GOVERNED_BY`
3. Relabel Module: `POST /api/graph/relabel {id, name}` → `MATCH (m) WHERE id(m)::text=$id SET m.name=$name`

**Safety invariants (copy-paste exact):**
```sql
-- Every Cypher write wrapped in SAVEPOINT, never bare
SAVEPOINT doc_mod_01;
SELECT * FROM cypher('hermes_knowledge', $$
  MERGE (m:Module {name: $name})
  -- property keys NO quotes: {name:'X'} not {"name":"X"}
  -- access via v.name not properties->>'name' (agtype)
  RETURN m.name
$$) AS (n agtype);
-- On success:
RELEASE SAVEPOINT doc_mod_01;
-- On exception:
ROLLBACK TO SAVEPOINT doc_mod_01;
```
- Use `MERGE` not `CREATE` for any entity that may exist.
- Bridge sync: after mutation affecting File/Module → `INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id::text, graph_name) VALUES ... ON CONFLICT DO NOTHING` or `DELETE WHERE vertex_id::text=$id::text` and verify `rowcount`.
- Never silently delete history: `DEPRECATES` keeps old node; break old `IMPORTS`/`GOVERNED_BY` but keep vertex; UI confirmation: `"2 files IMPORT this Module (`inDegree=2`). Delete edge anyway? [Break edge + keep history]"` with consequence `count()` preview.
- Hash-drift: `File.hash` vs `memory_entries` via `metadata->>'file_path'` + `hash` — `drift` computed server-side per node.

**Undo:** Client stack `undoStack.push({inverseOp})` — e.g., delete → inverse is MERGE same edge with same label.

---

## Theming, IDs, Rendering

- **Theming:** No hard bg. Use `getComputedStyle(document.documentElement).getPropertyValue('--foreground')` etc. Vars: `--foreground`, `--muted-foreground`, `--accent`, `--border`, `--card`, `--lapis`/`--turquoise`/`--amber` if present. Applies to `vis` option `groups` colors, edge color, background via CSS not `vis` option.
- **IDs String gate:** `network.body.data.nodes.getIds().every(id=>typeof id==='string')` — fail build if false.
- **Node rendering:** size `15 + degree*2 + pageRank*100` (cap 60), color by `communityId` (hash to palette), shape `dot` File / `square` Module / `triangle` Standard / `diamond` Dependency, border `dashed` width 3 if `drift`
- **Edge:** color `GOVERNED_BY:#7c6cf6, IMPORTS:var(--muted-foreground), DEPRECATES: dashed var(--amber)`, width `1 + weight`, arrows `to:{enabled:true, scaleFactor:0.6}`

---

## Safety Verification (TDD)

**Tests to write (tasks 3.2-3.3, 4.1):**
```python
def test_graph_api_returns_string_ids(client):
    r = client.get("/api/graph?limit=5")
    assert r.status_code==200
    for n in r.json()["nodes"]:
        assert isinstance(n["id"], str)
        assert "label" in n and "group" in n

def test_merge_preserves_history():
    r = client.post("/api/graph/merge", json={"fromName":"OldMod","toName":"NewMod"})
    assert r.status_code==200
    # Old still exists, New DEPRECATES Old
    assert cypher_count("MATCH (n:Module {name:'OldMod'}) RETURN count(n)")==1

def test_drift_marks_node(tmp_path):
    # modify file without reindex → drift:true
    p = tmp_path/"x.py"; p.write_text("changed")
    r = client.get("/api/graph?seed=x.py")
    assert any(n["drift"] for n in r.json()["nodes"] if n["path"].endswith("x.py"))
```

**Performance gate:** `ab -n 100 -c 10 http://127.0.0.1:7890/api/graph?limit=200` p95 <800ms (<2s target, 8s cap). Server log `limit` capped at 500.

---

## Files To Change

- Create: `src/hermes_memory/graph_api.py` (or extend `graph_view.py` — check existence, name router `graph_api`)
- Create: `tests/test_graph_pane_api.py`, `tests/test_graph_mutation.py`, `tests/test_drift.py`
- Create/modify: `src/hermes_memory/assets/fountain-modify.html` (pane) + `docs/research/swarm-2026-09-08/pane-preview.html` (throwaway)
- Modify: `sql/migrations/*` — add `File.pageRank`, `File.inDegree`, `Module.communityId`, `File.drift` if storing; else compute on fly (recommend compute+cache nightly job)

---

## Spike HTML Skeleton (copy-paste)

```html
<div id="mynetwork" style="width:100%;height:600px;border:1px solid var(--border)"></div>
<div id="inspect" style="position:absolute;right:0;top:0;width:320px;background:var(--card);border-left:1px solid var(--border)"></div>
<script type="importmap">{"imports":{"vis-network":"https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"}}</script>
<script>
const {DataSet, Network} = vis;
let nodes = new DataSet([]), edges = new DataSet([]);
let net = new Network(document.getElementById('mynetwork'), {nodes, edges}, {
  physics:{solver:'barnesHut', barnesHut:{gravitationalConstant:-8000}},
  nodes:{shape:'dot', scaling:{min:10,max:60}}, edges:{arrows:{to:{enabled:true}}}
});
async function load(seed=""){
  let r = await fetch(`/api/graph?seed=${encodeURIComponent(seed)}&hops=1&limit=200&includeStandards=true`);
  let j = await r.json();
  // Enforce String IDs
  if(j.nodes.some(n=>typeof n.id!=='string')) throw Error('IDs not strings');
  nodes.clear(); edges.clear(); nodes.add(j.nodes); edges.add(j.edges);
}
net.on('click', async p=>{
  if(p.nodes.length){ let id=p.nodes[0]; let r=await fetch(`/api/graph/node/${id}`); let n=await r.json(); document.getElementById('inspect').innerText=JSON.stringify(n,null,2); }
});
load();
</script>
```

---

*Commit after each TDD cycle; DoD per DEFINITION_OF_DONE.md Level 6 requires screenshot MEDIA: + curl proof, not sentence.*