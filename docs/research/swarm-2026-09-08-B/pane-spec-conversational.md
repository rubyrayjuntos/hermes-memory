# Path B Pane Spec — Conversational Graph (OC-MVP-3/4)

*Contract for implementer (zero context). Implements PANE_INTERACTION_SPEC.md §§1-3. Read-only, String-ID, Handler+match_route, 501 for control.*

---

## Visual Contract (Live, not codebase-ingest)

**Read endpoints (already in tree, 200 on :7890):**
- `GET /api/health` → drain_status aggregates + LEDGER
- `GET /api/librarian/graph/stats` → noun/edge counts, V9-gap
- `GET /api/librarian/graph/search?q=&k=&hops=1` → `pack_search` JSON `{graph:{nodes,edges,links}, retrieval:{funnel}, seeds}`
- `GET /api/librarian/nouns/{id}/hop` → 1-hop `{noun, neighbors:[{label,magnitude,provenance_turns,excerpts}], embed provenance, emptyReason}`

**Response (MANDATORY String IDs):** `id` is `"noun:123"` or `"age:..."` via `stringify_id` (`graph_view.py:27`) + `parse_vertex:68`. Never bare Number. Test `typeof id==='string'`.

**Error never blank:** Each `noun_hop` empty maps to spec literal `NO_CONNECTIONS` / `TURN_UNAVAILABLE` / `graph_degraded` / V9-gap / `EMBED_UNSTAMPED` (`graph_view.py:504`).

---

## Layouts

- **Conversational:** Force layout but sparse — adjacent-chain graph is not dense import mesh. Default vis-network `barnesHut` is fine; no Fountain flower (prompt-order, not meaning per CORRECTIONS #1).
- **Toggle:** Not needed V1 — single conversational view; Path A codebase view is separate file when deferred A lands.

---

## Inspect — Spec §2 Exact (fixes #80)

**Click node → fetch `GET /api/librarian/nouns/{id}/hop` → panel with 4 sections:**

1. **Identity:** `noun.label`, `noun.type`, degree (`val-1`).
2. **1-hop neighbors:** for each `semantic_edge` where `src_noun=id OR tgt_noun=id` and `verb_type='mentions'` → neighbor `label`, `magnitude`, `provenance_turns`.
3. **Embedding provenance:** `embed_model/dim` for vectors — if NULL render `EMBED_UNSTAMPED` (“trusted as nomic-768 by policy…”).
4. **Readable context:** for each edge's `provenance_turns`, `conversations_by_ids` → excerpt 280c (`store.py:660`).

**Empty table (never generic “no data”):**

| Condition → Literal (`graph_view.py`) | Source |
|----------------------------------------|--------|
| `count=0` → `NO_CONNECTIONS` | `semantic_edge` 0 |
| `provenance_turns` resolves 0 rows → `TURN_UNAVAILABLE` | turn not found |
| `conversations.drain_status='graph_degraded'` → degraded string | `02_schema:21` |
| `memory_chunk_nodes passport` NULL anti-join → V9-gap string | V9 gap |
| `embed_model/dim NULL` → `EMBED_UNSTAMPED` | trust_nomic_768 |

**Second click re-centers** same 1-hop on neighbor — multi-hop via sequential re-center (spec says 2-hop render is MVP-out).

---

## Modify — None in MVP

**All POST/PATCH/DELETE stay `501 read-only viz API`** per `RELEASE_CRITERIA.md OC-PROD-2`. Do not add `POST /api/graph/merge` etc. Control is Production with auth+confirmation+audit. SAVEPOINT discipline stays in doc, not in pane for B.

---

## Theming & IDs

- Vars: `var(--foreground)` etc., no hard bg (fountain.html pattern).
- IDs String gate: `nodes.every(n=>typeof n.id==='string' && n.id.startsWith('noun:'))` fail build if false.
- Node size `val=1+degree` (`_stamp_graph_degree`), color by type, edge width `magnitude/8`, arrows off (undirected co-mention).

---

## Safety Verification (TDD for B)

```python
def test_hop_returns_string_ids(client):
    r = client.get("/api/librarian/nouns/1/hop")
    assert r.status_code==200
    for n in r.json()["graph"]["nodes"]:
        assert isinstance(n["id"], str)
        assert n["id"].startswith("noun:")
def test_click_empty_has_reason(client):
    r = client.get("/api/librarian/nouns/99999/hop")
    assert "reason" in r.json() or "No connections yet" in str(r.json())
def test_funnel_present(client):
    r = client.get("/api/librarian/graph/search?q=test&k=8")
    assert "retrieval" in r.json()
```

**Perf:** `ab -n 100 -c 10 http://127.0.0.1:7890/api/librarian/graph/search?q=test` p95 <800ms (HNSW m=16).

---

## Files To Change (B only)

- Keep `src/hermes_memory/graph_view.py` helpers, `graph_http.py Handler`, `graph_runtime.py noun_hop/search` — no new router.
- Create `tests/test_hop_spec.py`, `tests/test_funnel.py`
- Modify `docs/graph/fountain.html` or new `docs/graph/conversational.html` to render spec §2 panel (split #hook raw into 4 sections).
- No `sql/migrations` for File.beamScore; no `graph_api.py` new POSTs.

---

## Spike HTML Skeleton (copy-paste, Handler-safe)

```html
<div id="graph" style="width:100%;height:600px;border:1px solid var(--border)"></div>
<div id="panel" style="position:absolute;right:0;top:0;width:360px;background:var(--card);border-left:1px solid var(--border);padding:12px"></div>
<div id="funnel" style="font:12px monospace"></div>
<script type="importmap">{"imports":{"vis-network":"https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"}}</script>
<script>
const {DataSet, Network} = vis;
let nodes=new DataSet([]), edges=new DataSet([]);
let net=new Network(document.getElementById('graph'), {nodes, edges}, {physics:{solver:'barnesHut'}});
async function loadHop(id){
  let r=await fetch(`/api/librarian/nouns/${id}/hop`); let j=await r.json();
  if(j.graph && j.graph.nodes.some(n=>typeof n.id!=='string')) throw Error('IDs not strings');
  document.getElementById('panel').innerText = JSON.stringify(j,null,2); // TODO split per spec §2
}
net.on('click', p=>{ if(p.nodes.length) loadHop(p.nodes[0].split(':')[1]); });
async function search(q){
  let r=await fetch(`/api/librarian/graph/search?q=${encodeURIComponent(q)}&k=8`); let j=await r.json();
  nodes.clear(); edges.clear(); nodes.add(j.graph.nodes); edges.add(j.graph.edges);
  document.getElementById('funnel').innerText = JSON.stringify(j.retrieval);
}
search("");
</script>
```

*Commit after each TDD cycle; DoD per DEFINITION_OF_DONE.md level-6 requires screenshot MEDIA: + curl proof.*
