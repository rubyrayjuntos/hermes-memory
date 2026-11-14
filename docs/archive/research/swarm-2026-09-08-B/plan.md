# Path B — Conversational Graph Visualize (OC-MVP-3/4) — Swarm Plan

> **For Hermes:** Re-oriented from mis-aimed codebase-ingest swarm (see ../swarm-2026-09-08/ flagged). Path B first per 2026-09-08 peer review.

**Goal:** Make the conversational-memory graph readable + interactable per `PANE_INTERACTION_SPEC.md` §§1-3 and `OC-MVP-3/4` — theory shapes noun/semantic_edge, data shapes hover/click/funnel, graph is first-class discovery of *what was said*.

**Scope:** `noun` (label/type/ag_vertex_id), `semantic_edge` (src/tgt, verb_type='mentions', magnitude, polarity, provenance_turns, e_src_vec/e_tgt_vec 768), `conversations.drain_status` + `embed_model/dim` (trust_nomic_768), `beam_score` query-time walk (`walk.py:76-91` c=src_align*tgt_align). No new AGE labels; no POST mutations (stay 501 per OC-PROD-2). Live reads only.

**Swarm (3 parallel, citation-gated >=8 https):**
- B1 Graph Theory for Conversations: centrality/community over noun co-mention, Leiden vs Louvain on semantic_edge, walk theory
- B2 Interaction Viz: hover identity, click 1-hop with provenance, funnel, empty-reason states per spec
- B3 Retrieval Walk: beam_score, HNSW, drain_status, unpassported gap, RQ-MVP

**Outputs:** docs/research/swarm-2026-09-08-B/{conversational-graph-theory-brief.md,interaction-viz-brief.md,retrieval-walk-brief.md,synthesis-matrix.md,pane-spec-conversational.md}
**Gate:** Every V1 WILL must `grep -R` live `src/` or `sql/` — no DDL-only label becomes a WILL.

