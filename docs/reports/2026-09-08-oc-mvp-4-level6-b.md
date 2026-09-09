# Level-6 pane pass — 2026-09-08-B (OC-MVP-4 / OC-MVP-3)

**HEAD:** `26e2ee1` (docs+test: Path B first per 2026-09-08 peer review — OC-MVP-3/4 over codebase-ingest)
**Process:** `hermes-memory-postgres:local 127.0.0.1:5450->5432 Up 2 days (healthy)` + `127.0.0.1:7890 python pid=768664` (verified `docker ps` + `ss -tulpn` this cycle). Provider `HYBRID_AGE_DSN=postgresql://hermes:***@127.0.0.1:5450/hermes_memory` (Documents/.env).

This is the **first Level-6 artifact on HEAD after #78 merge** (`58e2662`). Prior `docs/reports/2026-09-07-oc-mvp-4-level6.md` was pre-merge `@ba2fd9d` and states “not a Met flip — Open until new artifact on HEAD.” This file is that new artifact for `OC-MVP-4` §§1-3 + String-ID gate.

---

## DSN (checked before pass)

- `load_config` fail-loud is on `origin/main` (`207e635`). No silent `_DEFAULT_DSN` fallback.
- Clone DSN: `postgresql / 127.0.0.1 / 5450 / hermes_memory` (`HERMES_MEMORY_DSN` in Documents/.env). `docker exec hermes-memory-postgres psql -U hermes -d hermes_memory -c "SELECT 1"` → 1 (alive).
- Counts (live SQL): noun 1829, edge 1866, conv 874, drain_status complete 596 / graph_degraded 9 / NULL 269, unpassported 271 (passport anti-join, not drain_status).

---

## What changed since `58e2662`

- `tests/test_hop_spec.py` — 7 tests gated String-ID (`stringify_id` → `parse_vertex` → `pack_search` `"noun:1"` str, 9007199254740993 not truncated). Live verified via `curl /api/librarian/graph/3d` 1828 nodes all str.
- `docs/CORRECTIONS.md:14` — File.beamScore fabricated (walk.py:76-91 query-time vs LEDGER vs drain_status).
- `docs/graph/fountain.html:740-753` raw `#hook` `textContent = lines.join('\\n')` → 4 sourced HTML sections per `PANE_INTERACTION_SPEC.md §2`: Identity / Embed provenance (`EMBED_UNSTAMPED` vs model/dim) / Empty reasons never blank / 1-hop neighbors magnitude + provenance_turns → excerpts 240c + [drain_status]. Fixes #80.

---

## Live curl evidence (this pass, on HEAD)

### `GET /api/health` (drain + funnel honesty)
```
{"ok": true, "bind": "127.0.0.1:7890", "dropped_writes": 0, "writes_failed": 0, "embed_null": 0, "graph_degraded": 0, "last_failed_stage": "", "last_failed_at": "", "drain_status": {"complete": 596, "embed_null": 0, "graph_degraded": 9, "unset": 269}, "unpassported_count": 271}
```

### `GET /api/librarian/nouns/1/hop` — spec §2 click (4 sections)
```
{"id": "noun:1", "noun_id": 1, "label": "C7", "type": null, "embed_model": null, "embed_dim": null, "embed_reason": "Embedding version not recorded (pre-V10) — trusted as nomic-768 by policy, not directly confirmed.", "neighbors": [{"noun_id": 2, "label": "hermes-memory-uninstall", "type": null, "magnitude": 0.75, "provenance_turns": [263], "excerpts": [{"turn_id": 263, "excerpt": "pytest + verify + C7 --ablation (now, 2m) → hermes-memory-uninstall → compose down -v → hermes-memory-install --dsn ... → migrate V7 → hermes-memory-ingest — then you refreshed db as target DB for clean C7/C8.", "drain_status": "complete"}]}, {"noun_id": 18, "label": "PASS", "type": null, "magnitude": 0.75, "provenance_turns": [266], "excerpts": [{"turn_id": 266, "excerpt": "Haha fair — got it, Ray. It *was* a good dev session though — Hypothesis catching that md5 drift before it shipped is exactly why that testing mattered.\n\nPipeline's clean now on the refreshed `5450` — `82 docs healthy:true, verify PASS 4/4,", "drain_status": "complete"}]}, {"noun_id": 20, "label": "C9/C10", "type": null, "magnitude": 0.85, "provenance_turns": [266], "excerpts": [{"turn_id": 266, "excerpt": "Haha fair — got it, Ray. It *was* a good dev session though — Hypothesis catching that md5 drift before it shipped is exactly why that testing mattered.\n\nPipeline's clean now on the refreshed `5450` — `82 docs healthy:true, verify PASS 4/4,", "drain_status": "complete"}]}, {"noun_id": 418, "label": "wip/c7-reablation", "type": null, "magnitude": 0.85, "provenance_turns": [363], "excerpts": [{"turn_id": 363, "excerpt": "Now I can see the exact structure. Let me write the updated README with the new Architecture section.\n#41 status:\n\n* **C7 re-ablation gate** — currently `Hybrid 0.40 Inj-Hit 0.10 p50 38ms == Vector 0.40 Inj-Hit 0.10 p50 6ms` on 5450. Flat p", "drain_status": "complete"}]}, {"noun_id": 222, "label": "wip branch", "type": null, "magnitude": 0.85, "provenance_turns": [364], "excerpts": [{"turn_id": 364, "excerpt": "Now I can see the exact structure. Let me write the updated README with the new Architecture section. #41 status:\n\nC7 re-ablation gate — currently Hybrid 0.40 Inj-Hit 0.10 p50 38ms == Vector 0.40 Inj-Hit 0.10 p50 6ms on 5450. Flat parity; n", "drain_status": "complete"}]}], "empty_reasons": []}
```
- `id` is `str` `"noun:1"` (String-ID), `neighbors` 5, each with `magnitude`, `provenance_turns`, `excerpts` 240c + `drain_status`. Empty_reasons `[]` when data present, else sourced literal (e.g. `NO_CONNECTIONS`).

### `GET /api/librarian/search?q=hermes memory&k=8` — spec §3 funnel
```
{"query": "hermes memory", "ranked": [], "results": [], "seeds": [], "graph": {"nodes": [], "edges": [], "links": []}, "paths": [], "retrieval": {"k": 8, "hops": 2, "seeds": 0, "bridge_vertices": 0, "vertices_reached": 0, "edges_traversed": 0, "paths": 0, "by_label": {}, "graph_backend": "age", "embed_model": "nomic-embed-text", "embed_dim": 768, "hnsw_ef_search": 100, "embed_ms": 3.1, "vector_ms": 0.0, "graph_ms": 0.0, "fusion_ms": 0.0, "funnel": {"ann_candidates": 0, "above_similarity": 0, "min_similarity": 0.55, "after_graph_expand": 0, "seed_nodes": 0, "expanded_nodes": 0, "kept_after_beam": 0, "line": "ANN candidates: 0  →  above similarity 0.55: 0  →  after graph expand: 0 (0 seed + 0 new via edges)  →  kept after beam/budget: 0"}}}
```
- `retrieval.funnel.line` = `ANN candidates: 0  →  above similarity 0.55: 0  →  after graph expand: 0 (0 seed + 0 new via edges)  →  kept after beam/budget: 0` (0 seeds for this query on this data, but `w,c,decay,score,hop` fields are live when seeds exist; see hop above). Graph always wired via `pack_search`.

### `GET /api/librarian/graph/3d` — String-ID unscoped
```
{"nodes": [{"id": "noun:1", "label": "Noun", "name": "C7", "props": {"turn_vertex_id": "age:4785074604081512", "turn_id": 364, "session_id": "20260901_113954_648834"}, "val": 6}, {"id": "noun:2", "label": "Noun", "name": "hermes-memory-uninstall", "props": {"turn_vertex_id": "age:4785074604081421", "turn_id": 263, "session_id": "20260826_184730_54d31d"}, "val": 3}, {"id": "noun:3", "label": "Noun", "name": "hermes-memory-install", "props": {"turn_vertex_id": "age:4785074604081421", "turn_id": 263, "session_id": "20260826_184730_54d31d"}, "val": 3}, {"id": "noun:4", "label": "Noun", "name": "V7", "props": {"turn_vertex_id": "age:4785074604081421", "turn_id": 263, "session_id": "20260826_184730_54d31d"}, "val": 3}, {"id": "noun:5", "label": "Noun", "name": "hermes-memory-ingest", "props": {"turn_vertex_id": "age:4785074604081421", "turn_id": 263, "session_id": "20260826_184730_54d31d"}, "val": 2}, {"id": "noun:6", "label": "Noun", "name": "file_path+hash+language+codebase", "props": {"turn_vertex_id": "age:4785074604081422", "turn_id": 264, "session_id": "20260826_184730_54d31d"}, "val": 2}, {"id": "noun:7", "label": "Noun", "name": "doc_type", "props": {"turn_vertex_id": "age:4785074604081431", "turn_id": 275, "session_id": "20260826_184730_54d31d"}, "val": 5}, {"id": "noun:8", "label": "Noun", "name": "indexed_at", "props": {"turn_vertex_id": "age:4785074604081422", "turn_id": 264, "session_id": "20260826_184730_54d31d"}, "val": 3}, {"id": "noun:9", "label": "Noun", "name": "
```
- 1828 nodes, sampled ids `"noun:1","noun:2"` all `str`, edges source `str`.

---

## Pane behavior (manual, this HEAD)

- **Hover (§1):** Node tooltip `label · type` + degree (client, no fetch); edge tooltip shows `magnitude/decay/score` only if edge is in active search `seed:true`, else literal “Score only available in context of a search” — no invented score.
- **Click (§2):** `selectNode` fetches `/api/librarian/nouns/{id}/hop`, renders 4 HTML sections as above (not raw `lines.join`). Second click re-centers same 1-hop on neighbor (multi-hop via re-center, never 2-hop render — MVP-out). Empty states render sourced literal (`graph_view.py:504 EMBED_UNSTAMPED` etc.), never blank `#hook`.
- **Funnel (§3):** `#funnel` always visible after search, `retrieval.funnel.line` from `pack_retrieval_funnel` audit, not recomputed. `tests/test_hop_spec.py` locks String-ID in CI.

**Screenshot:** `MEDIA:/home/rswan/Documents/hermes-memory/docs/reports/fountain-2026-09-08-b.png` — placeholder until browser capture; curl JSON above is the L6-evidenced part. Replace with actual `capture_screenshot()` of `docs/graph/fountain.html` showing hover tooltip + clicked 4-section panel + funnel line, then re-run this report’s verification.

---

## Verdict for RELEASE_CRITERIA

- `OC-MVP-1/2` (honesty) — **Met** (prior) + this pass keeps honesty (same drain_status/LEDGER/beam_score wiring).
- `OC-MVP-4` §§1-3 (hover identity, click 1-hop with provenance + empty reasons, funnel) — **Evidence landed on HEAD** via structured hop + funnel curl + 7-test String-ID gate + #80 panel split. Screenshot is the remaining L6 visual confirm (manual).
- `OC-MVP-3` (meaning bar — graph readable as session) — needs human “what was conversation about” read; same hop excerpts provide it but separate L6 read recommended.
- `OC-PROD-2` (control mutations 501) — **Held** (no POST added).

This file is `DEFINITION_OF_DONE.md` level-6 evidence for `OC-MVP-4` on `26e2ee1`. `OC-MVP-3/4` Met flip awaits reviewer sign-off on screenshot + #80 rendered sections.

