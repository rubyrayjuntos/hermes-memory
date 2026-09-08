# Pane interaction spec — MVP

Created: 2026-09-07 · Last modified: 2026-09-07 · Status: MVP decisions, binding

This document exists because the pane's interaction behavior has been implied,
not specified, across this project's whole history — and that gap has cost
real diagnostic time. Everything below is a decision, not a suggestion. If
the running pane does something different from this document, the pane is
wrong (or this document is stale and needs a dated edit — never both treated
as simultaneously true).

**Companion artifacts (not the live pane):**
[`docs/pane-prototype.html`](pane-prototype.html) and
[`docs/hermes_librarian_traversal_expansion_simulator.html`](hermes_librarian_traversal_expansion_simulator.html)
— local HTML that settled hover/click/search and the §5 vector/graph split.
They do not render on github.com. The live inspector is
[`docs/graph/fountain.html`](graph/fountain.html) on `:7890`.

**Governing rule, inherited from `OC-MVP-1`/`OC-MVP-2`:** every value shown
anywhere in the pane traces to one real field. No local recomputation of a
score. No invented placeholder when data is missing — an explicit stated
reason instead. This document is that rule applied to every interaction, not
just the static view.

---

## 1. Hover — identity only, no query

Hovering a node or edge is a glance, not a commitment. It must be instant
(no network round-trip beyond what's already loaded for the current view).

| Element | Hover shows | Source |
|---|---|---|
| Node (noun) | `label`, `type`, degree (count of edges touching it in the currently loaded graph) | `noun.label`, `noun.type`; degree computed client-side from already-fetched edges, not a new query |
| Edge | If this edge is part of an active search result: `magnitude`, `decay`, and the `beam_score` component values exactly as returned by that search's debug payload. If NOT part of an active search (plain browsing): **"Score only available in the context of a search — run a query to see relevance."** | Search debug payload when present; explicit no-op message when absent |

**Never:** compute or estimate a score for an edge outside a search context.
An edge browsed without a query has no relevance score to any query — don't
invent one by, e.g., using its raw `magnitude` alone as if it were a
`beam_score`.

---

## 2. Click a node — 1-hop expansion, real data, or a real reason why not

This is the core of what was missing. Clicking a node commits to a real
query and must answer, honestly, "what does this node connect to and why."

**On click, fetch and show:**

1. **Identity.** `noun.label`, `noun.type`.
2. **1-hop neighbors.** Every `semantic_edge` row where this noun is `src_noun`
   or `tgt_noun`, `verb_type='mentions'` (current schema — update this line
   the day bi-temporal `valid_to` filtering lands, per `CORRECTIONS.md` #7).
   For each neighbor: the neighbor's `label`, the edge's `magnitude`, and the
   edge's `provenance_turns`.
3. **Embedding provenance, not raw vectors.** Show `embed_model` / `embed_dim`
   for this noun's stored vector — confirms what produced it. Do **not**
   render raw 768-dimensional floats; that's not readable context, it's noise.
   If `embed_model`/`embed_dim` is `NULL` on this row: state plainly
   **"Embedding version not recorded (pre-V10) — trusted as nomic-768 by
   policy, not directly confirmed."** This is `trust_nomic_768` made visible
   instead of silent.
4. **Readable context.** Pull the actual turn text via `conversations_by_ids`
   for the edge's `provenance_turns`, and show a short excerpt per turn — the
   real sentence(s) that produced this connection, not just the noun labels.
   This is the difference between "these two things are linked" and "here is
   why."

**Explicit empty/failure states — never a blank panel:**

| Condition | What the panel says |
|---|---|
| Node has zero edges | "No connections yet — mentioned once, not yet linked to anything else." |
| Edge exists but its `provenance_turns` resolve to no rows | "Connected, but the source turn is unavailable (turn `<id>` not found)." |
| The turn behind this node has `drain_status = 'graph_degraded'` | "Extraction failed for this turn — this connection may be incomplete." |
| The turn behind this node is in the historical unpassported set (V9 gap, pre-backfill) | "This turn predates full extraction and was never linked — see `RQ-PROD-2`." |
| Embedding version unstamped | See item 3 above. |

A blank or silently-empty panel is never an acceptable outcome of a click.
Every non-result has a stated, specific reason, sourced from a real field
(`drain_status`, `provenance_turns`, `embed_model`), never a generic
"no data" placeholder.

**Second click / re-center (MVP-in):** clicking a neighbor node re-centers
the same 1-hop view on it. This gives multi-hop exploration without ever
rendering an unbounded graph at once. **Multi-hop-in-one-view (rendering 2+
hops simultaneously from a single click) is MVP-out** — defer explicitly,
don't imply it.

---

## 3. Verifying the "shape" of retrieval — the funnel, always visible

This is the standing complaint this spec exists to close: there has been no
way to see, from the UI, what actually happened during a search — how many
candidates existed, how many survived each filter, why the final set looks
the way it does.

**After any search/prefetch, the pane must show a funnel readout, not just
final results:**

```
ANN candidates: 12  →  above similarity 0.55: 6  →  after graph expand: 8 (6 seed + 2 new via edges)  →  kept after beam/budget: 2
```

Every number in that line must come from the same values already logged by
the real pipeline (the pattern already exists informally — `prefetch
seeds=2 graph=6 kept=2` appeared in this thread's own logs tonight). This
funnel is not a new computation; it's surfacing numbers the code already
produces, in the UI instead of only in a log file.

**Visual distinction of result nodes (MVP-in, simple):**

- **Seed nodes** (direct ANN match) — one visual treatment.
- **Expanded nodes** (reached via graph walk from a seed, not a direct match)
  — a distinguishable treatment, labeled with hop distance (1-hop, 2-hop).
- **Dropped candidates** — do not render as ghosted nodes in the graph
  (MVP-out, adds real rendering complexity for low value). Their count is
  captured in the funnel readout above; that's sufficient at MVP.

---

## 4. Explicit MVP scope line

**In, decided:**
- Hover: identity + degree (nodes), score-in-search-context-only (edges).
- Click: 1-hop neighbors, edge magnitude, embedding provenance, readable
  turn excerpts, explicit reasons for every empty/failure state.
- Re-center on second click for informal multi-hop browsing.
- Always-visible retrieval funnel after any search.
- Seed vs. expanded visual distinction, by hop.

**Out, deferred — named so it cannot be silently assumed to exist:**
- Simultaneous multi-hop rendering (2+ hops in one view from one click).
- Editing, annotating, or deleting anything from the pane (`OC-PROD`, gated
  behind auth per `RELEASE_CRITERIA.md`).
- A projected embedding-space view (meaning-driven clustering layout to
  eventually replace the flower's screen space) — real, valuable, separately
  scoped work; not this document.
- Live-push updates while a conversation is ongoing. A manual refresh is
  MVP-sufficient.
- Rendering ghosted/dropped candidate nodes.

If a future session or agent implements or describes pane behavior not
listed here, that behavior is either undocumented drift (fix by adding it
here, dated) or scope creep past MVP (flag it, don't just ship it).

---

## 5. Vector/graph relationship — the dual-space model

This section is the decision record for how (and whether) vector embeddings
are ever allowed to influence graph structure or layout. It exists because
this question came up repeatedly and was answered piecemeal in
conversation; this is the one place the answer now lives.

### The two spaces, as currently built (verified, not proposed)

1. **Graph space (structural fact and provenance).** `semantic_edge`
   connectivity is built entirely from co-occurrence at write time — two
   nouns mentioned adjacently in one turn. Edge `magnitude` reflects how
   often and strongly two concepts were explicitly paired. Nothing about
   this space is influenced by vector similarity.
2. **Vector space (fuzzy retrieval and trust).** Whole-turn embeddings
   route a query to entry points (`vector_search`, ANN over
   `conversations`/`memory_entries`/`doc_chunks`). Once inside the graph,
   an edge's own stored vectors (`e_src_vec`/`e_tgt_vec`) are compared
   against the query to weight trust (`beam_score`'s `src_align`/
   `tgt_align`). **Vector similarity never creates, removes, or reroutes a
   graph edge.**

This separation is deliberate: an edge existing only when real conversation
provenance backs it is what keeps the walk auditable and prevents false
grounding (`docs/CORRECTIONS.md` #1). The known cost is vocabulary
fragility — two nodes representing the same real-world thing stay
unconnected islands if never explicitly co-mentioned (demonstrated directly
in the prototype: `nightingale` and `atlas` share no edge despite both
being mentioned in the same real conversation, because extraction is
adjacent-pairs-only — issue #72).

### Current layout (as built): topology + magnitude only

The prototype's force layout uses graph topology and edge `magnitude` as
the only inputs — no vector influence at all. This is **not** any of the
three levels below; it is the current, un-decided baseline they'd be
layered on top of.

### Three levels of deeper vector integration — status, not a spec

If vectors are ever allowed to shape layout or topology, these are the
options, ordered by how much they touch the audit-provable core.

| Level | Mechanism | Status |
|---|---|---|
| **1 — Pure visual clustering** | Run UMAP/t-SNE over per-node embeddings to set base layout position; draw real co-occurrence edges on top. Layout reflects topic similarity; the graph itself is untouched. | **Not decided.** Not what the current prototype does. Requires a stored per-node embedding (see gap below). |
| **2 — Dual-force physics** | Keep structural springs (topology + magnitude) as-is; add a second, weaker attraction force between *any* two nodes based on embedding similarity — pulling semantically related-but-unconnected nodes closer without drawing a line between them. | **Not decided. Blocked on a real infra gap** (below) beyond being undecided. |
| **3 — Virtual/inferred edges** | Use vector similarity to propose connections beyond real co-occurrence. Two distinct variants, with different outcomes — see below. | **Query-time variant: rejected. Offline variant: tested, deferred.** |

**Correction to the mechanism as originally proposed for Level 2:** it was
described as computing similarity via `cosine_similarity(e_src_vec,
e_tgt_vec)`. Those fields belong to one specific *existing* edge — they
can't be used to compute similarity between two nodes that have no edge
between them, which is the entire point of this force (pulling unconnected
nodes together). This level requires a stored **per-noun** embedding
(`noun.embedding` or equivalent), which does not exist in the schema today
— `noun` currently has no embedding column at all. This is the same gap
the Level 3 offline experiment (below) had to work around by embedding
labels fresh, on the fly, rather than reading a stored value. Level 2 is
blocked on this infrastructure independent of whether it's decided to be a
good idea.

**Level 3 has two variants that must not be conflated:**

- **Query-time kNN jumps during the graph walk** — vector similarity lets
  the beam walk leap to an unconnected node mid-query. **Rejected**, not
  merely deferred: cost (an ANN call per beam step, not per query, against
  a `prefetch_timeout_s` budget already measured at p95 ≈ 1.9s) and trust
  (a similarity leap surfaced with the same confidence as a real edge risks
  false grounding — the model asserting an unspoken association as
  remembered fact).
- **Offline batch-proposed edges** — a scheduled job proposes candidate
  edges from similarity, tagged with a separate `edge_origin` column
  (`observed` vs `inferred`, independent of any temporal-validity columns —
  never share one field between "is this still true" and "how do we know
  this at all"). **Empirically tested** — see
  `docs/wiki/experiment-similarity-inferred-edges.md` (or wherever it lands
  in the wiki). Result: real, measured lift over chance (~38x pooled), but
  small in absolute terms (pooled ~0.61% candidate confirmation vs ~0.016%
  control). **Deferred as a product decision** — not worth the schema and
  trust-labeling cost at this project's data volume, pending Phase 3
  (hyperbolic embedding) as a potentially cleaner fix for the same
  underlying problem. Full reasoning and reconsider-triggers are in the
  experiment brief, not duplicated here — this spec cites it, it doesn't
  restate it.

### What this means for the pane, concretely

- Node position: topology + magnitude only. No vector-driven layout exists
  or is scheduled.
- Any future Level 1/2 work needs `noun.embedding` built first — track as
  its own infra item, not bundled into a layout feature.
- Inferred edges, if ever built per the offline variant, must render
  visually distinct from real edges in the pane (a dashed or differently-
  colored treatment, analogous to the prototype's turn-space hand-off
  lines) — never presented with the same visual weight as a witnessed
  connection. This is a hard requirement, not a style preference, given the
  false-grounding risk named above.

