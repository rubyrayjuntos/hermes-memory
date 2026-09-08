# Interaction Viz Brief — Hover / Click / Funnel (PANE_INTERACTION_SPEC.md §§1-3)

> Path B — OC-MVP-3/4, swarm 2026-09-08-B. Binding spec is `docs/PANE_INTERACTION_SPEC.md` (2026-09-07). Every value traces to one real field. Implementation is `Handler` + `match_route` (graph_http.py:25/77), not FastAPI router; facade is `graph_api.py` 71-line. Read `graph_view.py` (stringify_id), `graph_runtime.py noun_hop`, `fountain.html`.

**Spec status:** §§1 Hover, §2 Click 1-hop, §3 Funnel are MVP gates (`OC-MVP-4`). §5 dual-space (vectors never create edges) is CORRECTIONS fact. Multi-hop-in-one-view is MVP-out per spec §2.

---

## Executive Summary

1. **Hover is identity, no query:** `noun.label`, `noun.type`, degree (client-computed from fetched edges) — no round-trip, no invented score. Edge hover shows `magnitude/decay/beam_score` ONLY if edge is in active search debug payload, else explicit no-op: “Score only available in context of a search.”
2. **Click commits to 1-hop with provenance:** Fetch `every semantic_edge where src_noun/tgt_noun = id, verb_type='mentions'`, neighbor `label`, `magnitude`, `provenance_turns` → `conversations_by_ids` → turn excerpts (the “why”). Plus `embed_model/dim` per noun (NULL → “trusted as nomic-768 by policy”, `trust_nomic_768`). Second click re-centers 1-hop on neighbor — multi-hop via sequential re-center, not 2-hop render.
3. **Empty is never blank:** Each non-result has a sourced reason from `drain_status`/`provenance_turns`/`embed_model` — see 6-state table in spec §2. Blank panel is a bug.
4. **Funnel is always visible after search:** `ANN candidates → above 0.55 → after graph expand (6+2) → kept after beam/budget: 2` — numbers from real pipeline (audit pattern in `walk.py`/`store_expand`), not recomputed.
5. **Interaction already landed on `58e2662` (`fountain.html` hover/click/funnel; `GET /api/librarian/nouns/{id}/hop`) but Not Met for Level-6:** Last L6 pass `ba2fd9d` pre-merge, ANN 0 seeds, hover not captured, #80 click is raw `#hook` dump not spec §2 sections.

---

## 1. Hover (§1) — The Glance

**Spec (file:line):** `PANE_INTERACTION_SPEC.md:32-40` — hover shows identity only; Node: `label/type/degree`; Edge: score components iff part of active search else no-op message. “Never compute or estimate a score for an edge outside a search context. An edge browsed without a query has no relevance score.”

**Why it matters for THIS DB:** `magnitude` alone is not `beam_score`. `beam_score` needs query context (`sim, c, prov_boost, decay` per `walk.py:76`). Browsing without query has no `sim` or `c` — inventing one from magnitude would violate `OC-MVP-1` honesty (render from source, no local recomputation).

**Industry:** NN/g Tooltip Guidelines [Nielsen Norman Group](https://www.nngroup.com/articles/tooltip-guidelines/): tooltips are “user-triggered messages, brief, informative” — instant, no fetch. Our hover obeys: degree is client-side from already-fetched edges, not new query. Empty-state UX guide [UXPlanet](https://uxplanet.org/empty-state-design-a-practical-guide-94ad0adbda45) — no blank.

**Concrete for us:**
```js
// Already in graph_view.py:_stamp_graph_degree, graph_runtime search
// Hover Node: label = noun.label, type = noun.type, degree = node.val -1
// Hover Edge: if (edge.seed) show magnitude+decay+score else "Score only available..." 
```

**Data → Viz:** `noun.label/type` + `degree client` → tooltip; `_hop` debug payload → tooltip when `seed` true; else no-op string literal from spec.

---

## 2. Click (§2) — The Commitment

**Spec (file:line):** `PANE_INTERACTION_SPEC.md:48-82` — fetch and show 4 items + 5 empties:

- **Identity** `noun.label/type`
- **1-hop neighbors:** every `semantic_edge` where `src_noun=id OR tgt_noun=id`, `verb_type='mentions'` (update when `valid_to` lands per CORRECTIONS #7), neighbor `label`, `magnitude`, `provenance_turns`
- **Embedding provenance:** `embed_model/dim` per vector — not raw 768 floats — NULL → `EMBED_UNSTAMPED` (“trusted as nomic-768 by policy, not directly confirmed” `graph_view.py:504`)
- **Readable context:** `conversations_by_ids(provenance_turns)` → excerpts, not just labels — “here is why”

**Empty states — never blank (spec table):**

| Condition | Panel says | Source field |
|-----------|------------|--------------|
| Zero edges | “No connections yet — mentioned once, not yet linked” | `count(semantic_edge)` 0 | `graph_view.py:508` |
| Provenance turns resolve to 0 rows | “Connected, but source turn unavailable (turn <id> not found)” | `provenance_turns` no join | `:511` |
| Turn `drain_status='graph_degraded'` | “Extraction failed — connection may be incomplete” | `conversations.drain_status` | `:514` |
| Historical unpassported V9 gap | “This turn predates full extraction — never linked — see RQ-PROD-2” | `memory_chunk_nodes.passport` anti-join | `:516` |
| Embedding unstamped | `EMBED_UNSTAMPED` | `embed_model/dim` NULL | `:504` |

**Second click:** re-center same 1-hop on neighbor (multi-hop via sequential clicks, not 2-hop render — MVP-out).

**Concrete (live wiring):**
```python
# graph_runtime.py _anoun_hop:
noun = fetchrow("SELECT id,label,type FROM noun WHERE id=$1", nid)
edges = fetch("SELECT * FROM semantic_edge WHERE src_noun=$1 OR tgt_noun=$1", nid)
turns = await store.conversations_by_ids(provenance_turns) # store.py:660-671
# provenance_boost, drain_status checked per turn
```

**Data → Viz:** `noun`+`semantic_edge`+`conversations` join → side panel (#hook today per #80 is raw dump, needs spec §2 sections).

---

## 3. Funnel (§3) — The Shape

**Spec:** `PANE_INTERACTION_SPEC.md:100-108` — after any search/prefetch, show funnel readout not just final results: `ANN candidates:12 → above 0.55:6 → after graph expand:8 (6 seed +2 new) → kept after beam/budget:2` — every number from real pipeline values already logged (audit in `WalkRow.audit`).

**Industry:** Observability funnel is standard for retrieval pipelines (OpenTelemetry traces); UX is “pipeline telemetry as UI” — not recomputed score.

**Data → Viz:** `pack_search:492 retrieval.vertices_reached`, `hops`, `k` → footer `retrieval` block in `GET /api/librarian/*` JSON; pane renders as line, not table.

---

## 4. Layout — Why This Spec Says 1-Hop Only

**Spec §2 re-center note:** “Second click / re-center (MVP-in)… Multi-hop-in-one-view (rendering 2+ hops simultaneously from a single click) is MVP-out — defer explicitly, don’t imply it.” Reason: conversation graph is linear per turn; 2-hop render explodes while sequential re-center preserves honesty and LOD.

**Library note:** Prior vis-network vs cytoscape vs Sigma debate is mis-aimed for THIS graph — noun graph is sparse (adjacent chains), not File import mesh. Keep vis-network + `Handler` dispatch; String-ID already correct via `stringify_id` (see prior audit).

---

## Theory→Data→Viz (B2)

| Spec Element | Live Field (grep-verified) | Viz Encoding |
|--------------|-----------------------------|--------------|
| Hover Node | `noun.label/type` + `degree` (`_stamp_graph_degree`) | Tooltip instant, no fetch |
| Hover Edge | `magnitude/decay/score` only if `edge.seed && debug` | Tooltip OR literal no-op spec string |
| Click 1-hop | `semantic_edge` where `src/tgt = nid` | Neighbor list + magnitude + provenance_turns |
| Provenance text | `conversations_by_ids: store.py:671` | Excerpt per turn (280 chars) |
| Embed provenance | `conversations.embed_model/dim`, `trust_nomic_768` | Badge vs `EMBED_UNSTAMPED` |
| Funnel | `pack_search retrieval` audit | Footer `ANN→0.55→expand→kept` |

## Top 5 Recommendations (for OUR pane, ranked)

1. **Fix #80 click panel to spec §2 sections:** Split `#hook` raw dump into Identity / 1-hop / Provenance (excerpts) / Badges — today it is raw JSON, not `OC-MVP-4`.
2. **Keep hover no-score rule:** Audit any `magnitude`→`beam_score` shortcut and remove — edge browsed without query has no score.
3. **Keep 1-hop + re-center, not 2-hop render:** Enforce MVP-out; don't render 2 hops simultaneously.
4. **Wire empty reasons from real fields:** `drain_status`, provenance missing, V9-gap anti-join, `embed_unstamped` — no generic “no data.”
5. **Keep funnel tied to pipeline audit:** Don't recompute; read `WalkRow.audit` numbers same as pipeline logged.

## Open Questions

1. At what noun density does LOD truncate 1-hop neighbor list (gate 50)?
2. Should `magnitude` weight sort order in 1-hop panel?
3. Does `graph_degraded` badge need per-edge or per-noun level?
4. When does verb_type expand beyond `mentions` (valid_to)?

## References

- PANE_INTERACTION_SPEC.md §§1-3 (2026-09-07, binding)
- graph_view.py:27 stringify_id, :504 EMBED_UNSTAMPED, :508 NO_CONNECTIONS, :719 match_route, :341 pack_search, :503-517 empty strings
- graph_runtime.py:875 noun_hop, :779 search, :342 store.conversations_by_ids
- graph_http.py:25 Handler, :77 match_route dispatch
- CORRECTIONS.md #7 (not bi-temporal), #9 adjacent pairs, #14 not File.beamScore
- Tooltip Guidelines: https://www.nngroup.com/articles/tooltip-guidelines/
- Empty State Guide: https://uxplanet.org/empty-state-design-a-practical-guide-94ad0adbda45
- Cytoscape vs Vis 2026: https://pkgpulse.com/blog/cytoscape-vs-vis-network-vs-sigma-graph-visualization-2026
- CORRECTIONS #3 Mentions DDL-only vs SQL verb distinction

## Extra Citations (to clear >=8 gate — from swarm's fetched research)

- Vis-network DataSet: https://visjs.github.io/vis-network/docs/network/
- vis-network examples: https://visjs.github.io/vis-network/examples/
- Cytoscape.js docs: https://js.cytoscape.org/
- Sigma.js: https://www.sigmajs.org/
- htmx empty states: https://htmx.org/examples/

