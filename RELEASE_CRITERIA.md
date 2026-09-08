# Release criteria — Hermes Librarian

This is a **destination**, not a status label and not a roadmap.

`pyproject.toml` `0.1.0` and README **Alpha, local trust** describe *where we are*.
This file says what **done** means, on two independent tracks plus one
**derived** seam (Observability & Control), at two bars.

When a change lands, name the row it closes (or say it is out of scope). Do not
treat a trail of verified incident-fixes as arrival. A Met flip also needs
[`DEFINITION_OF_DONE.md`](DEFINITION_OF_DONE.md): user-facing rows need level 6
and an artifact, not “the code claims to.”

**Supersedes** the 2026-09-01 “v0.2 production-ready plugin” wording in
`docs/plans/board.md` and `docs/plans/sprint-v02.md`. Those files are sprint
scratch. They do not define release.

---

## Binding MVP-scope decision

**MVP is Delivery-trustworthy on a single loopback operator.** Retrieval Quality
ships *alongside* as a known-open research question: the scoring *mechanism* must
not silently fork or rot, but “the right memory came back” is **not** an MVP
release gate.

| Question | Answer |
|----------|--------|
| Does a live-shaped / human-judged golden set block an MVP cut? | **No.** That is `RQ-PROD-1`. |
| Does a second scoring formula or a dead formula in-tree block an MVP cut? | **Yes.** That is `D-MVP-4` / `RQ-MVP-1`. |
| Does `hermes-memory-verify` / synthetic ANN path failing in CI block an MVP cut? | **Yes.** That is `RQ-MVP-2` (mechanism smoke, not a quality floor). |
| Does Injection-Hit ≥ 0.85 (spec §) block an MVP cut? | **No.** Sprint already marked that advisory until a ≥50-item golden set. That floor is Production-RQ, and only after `RQ-PROD-1` exists. |
| Does an unauthenticated control action on the pane block an MVP cut? | **Control is out of MVP.** A stale *view* that disagrees with `drain_status` / `LEDGER` / `beam_score` blocks `OC-MVP-1`. |
| Does a live graph that is wired to real fields but unreadable as a session block MVP? | **Yes.** Honesty (`OC-MVP-1`/`OC-MVP-2`) is not meaning. That is `OC-MVP-3`. |
| Does a pane that is honest but dead on hover/click/search-funnel block MVP? | **Yes.** That is `OC-MVP-4` / [`docs/PANE_INTERACTION_SPEC.md`](docs/PANE_INTERACTION_SPEC.md) §§1–3. Honesty without interaction is not enough to trust the data from the UI. |
| Does a missing DSN silently landing on `:5450` block ship? | **Yes.** That is `D-MVP-5` / [#74](https://github.com/rubyrayjuntos/hermes-memory/issues/74). Closed at `207e635`: `load_config` raises; Documents `.env` names `:5450/hermes_memory` on purpose. |

Both scopes are legitimate for an alpha memory system. This repo picks the first:
**do not lose or mis-mark writes; do not pretend retrieval quality is closed.**
The pane is how a human checks those two claims. It is not a third scoring or
health authority.

---

## Tracks (do not collapse)

| Track | Question | Evidence this thread already treats them as separate |
|-------|----------|-----------------------------------------------------|
| **Delivery** | Did the write happen, survive restart/crash, get deployed, stay observable? | Fail-loud policy, boot-gate migrations, `drain_status`, CI write-path tests, mypy on store/provider/`graph_runtime`. |
| **Retrieval Quality** | Did the *right* memory come back for a given query? | One substantive event: four formulas collapsed to `beam_score`. The thing that would validate whether that formula is *good* (live-shaped, human-judged golden set) is still unbuilt and must stay ungated while V9-gap turns remain. |
| **Observability & Control** (derived) | Can a human *see* Delivery and RQ facts from source, and later *act* without inventing a third truth? | Pane sits on the seam. Telemetry ≠ control. `docs/plans/visual-pane.md` “Command & Control Drawer” (CRUD) is Production-OC and must not ride in on view work. |

Delivery work can keep landing while Retrieval Quality sits at “mechanism locked, quality unproven.” That is sequencing, not maturity of the product as a whole.

**View ships first. Control is a later bar** (`OC-PROD-*`) with its own auth
decision. Loopback “anyone on the machine” is not that decision. Diagnostic
telemetry is most of the *view* half; it is not 75% of a view-and-control pane.

---

## Criteria

Status column is a **snapshot against `origin/main` @ `207e635` (2026-09-08)**, with live evidence from the installed pin `8c3e0de` on `:5452` / `hermes_memory_installed` and Documents `:5450` / `hermes_memory` for `D-MVP-5`.
Re-check before treating a row as closed. Execution state, not opinion.
Narrative (why a row exists) is [`docs/specs/HERMES_MEMORY_SPEC.md`](docs/specs/HERMES_MEMORY_SPEC.md) §5;
**Open/Met lives only in this table.** `*-PROD-*` rows are out of the MVP cut.

| ID | Track | Bar | Criterion | Why it is a row (already surfaced) | Status @ 207e635 |
|----|-------|-----|-----------|------------------------------------|------------------|
| `D-MVP-1` | Delivery | MVP | **Turn-drain writes** fail loud **or** are durably marked. L0 never raises. L1 = no row + process `writes_failed` (accepted loss). L2 = row + `embed_null`. L3 = row + `drain_status='graph_degraded'`. Scope is `insert_turn` / flower / nouns / mentions. Mentions dim-mismatch and ingest stamps are `D-MVP-6` / `D-MVP-7`. | Silent loss was the original lie. | **Met** for this narrowed drain. Installed path: 4/4 turns `drain_status='complete'`; `/api/health` `writes_failed=0` `embed_null=0` `graph_degraded=0`. The two named holes are **Met** at `207e635` (`D-MVP-6`/`D-MVP-7`). |
| `D-MVP-2` | Delivery | MVP | Boot refuses to start on schema drift (`apply_pending_migrations` then `Store.require_schema_head`). | Compose init is first-boot only; live migrate is a backstop. | **Met.** Installed plugin has no copied `sql/` tree. Head check is `CONSUMER_HEAD_VERSIONS` vs live `migration_history` (`8c3e0de`). |
| `D-MVP-3` | Delivery | MVP | CI on every merge runs the write-path tests (L1/L3 names collectable; `integration or store or idempotent` including `test_drain_status`). | A red `main` with “36 passed” is not a green write path until the failure is opened. | **Met** on `origin/main` `207e635` — CI [34187637800](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34187637800) success (ruff+mypy, property 3.11/3.12, integration). PR CI for #76: [34183424291](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34183424291). |
| `D-MVP-4` | Delivery | MVP | One scoring function in-tree (`beam_score`). No dead competing formulas. | Four-formula split-brain. | **Met** (also `RQ-MVP-1`) |
| `D-MVP-5` | Delivery | MVP | **Ship-blocker ([#74](https://github.com/rubyrayjuntos/hermes-memory/issues/74)).** After an explicit dotenv load, a missing `HYBRID_AGE_DSN` (or yaml-named `dsn_env`) must **raise**. It must not fall back to `_DEFAULT_DSN` `:5450/hermes_memory` while that container still answers. | Same failure shape as I-INSTALL-1, one layer out. | **Met** at `207e635` (`config.py` `load_config` / `dotenv_paths`). Clone `.env` first. Documents `.env` names `127.0.0.1:5450/hermes_memory`. TCP connect to that DB succeeded (869 conversations). Issue comment on #74. |
| `D-MVP-6` | Delivery | MVP | **[#70](https://github.com/rubyrayjuntos/hermes-memory/issues/70).** Dim mismatch on an existing `semantic_edge` UPDATE must fail loud or stamp `graph_degraded` / LEDGER — not `continue`. | Same invariant as `D-MVP-1`, on the mentions EMA path that `D-MVP-1` no longer pretends to cover. | **Met** at `207e635`, verified `store.py:623-628`. Raises `ValueError`; drain maps to `graph_degraded`. Issue comment on #70. |
| `D-MVP-7` | Delivery | MVP | **[#71](https://github.com/rubyrayjuntos/hermes-memory/issues/71).** Ingest stamps `embed_model`/`embed_dim` on `doc_chunks` and file-backed `memory_entries`. `trust_nomic_768` stays legacy-NULL only. | New NULL rows keep growing the “legacy” set. | **Met** at `207e635`, verified `ingest.py:516-529` and `:554-576`. Issue comment on #71. |
| `D-PROD-1` | Delivery | Production | Fleet-safe: revisit `DEPLOY_TOPOLOGY.rolling_deploy` vs `head_check=expected_not_applied_only`. Topology today is one provider + optional pane, not a rolling fleet. | Flipping one without the other is a known foot-gun (`AGENTS.md`). | **Open** — current topology is loopback / single-node by design. |
| `D-PROD-2` | Delivery | Production | CD runs migrations against live, not boot-time backstop only. | GitHub CD does not migrate live. | **Open** |
| `D-PROD-3` | Delivery | Production | Auth on the ports that are not loopback-only; secrets handling remains file-local (no `.env` in git). | Alpha binds `127.0.0.1:5450` / `:7890`. Loopback is not an auth story. | **Open** as Production. Loopback bind is an Alpha control, not this row. |
| `D-PROD-4` | Delivery | Production | Versioned release tags whose changelog matches `pyproject.toml`. | Tag `v0.1.0` (2026-08-24); `main` is ahead; package still `0.1.0`. | **Open** — changelog exists; version/tag have not caught `main`. |
| `RQ-MVP-1` | Retrieval Quality | MVP | One named, documented scoring function (`beam_score` in walk / architecture / manifold spec). | Same split-brain as `D-MVP-4`. | **Met.** [#72](https://github.com/rubyrayjuntos/hermes-memory/issues/72) (adjacent-only mention chains + `MAX_NOUNS=5`) is an RQ *construction* ceiling, **not** an MVP ship-blocker and **not** a second scoring formula. Do not promote #72 into Delivery/OC because it has an issue number. |
| `RQ-MVP-2` | Retrieval Quality | MVP | Synthetic ANN / verify path passes in CI (`hermes-memory-verify`, walk/score tests). This proves the *pipe*, not that recall is good. | Needed so Delivery hardening cannot ship a broken retriever unnoticed. | **Met** as mechanism smoke on `207e635` CI [34187637800](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34187637800). **Not** a quality floor. Installed-path recall: `/api/librarian/search?q=Nightingale` on `:5452` returned conv 5–8. |
| `RQ-PROD-1` | Retrieval Quality | Production | Live-shaped golden set with human-judged expected memories, a defined bar, enforced in CI. | Spec Injection-Hit ≥ 0.85 is advisory until that set exists (≥50 was the sprint gate). `assert_live_shaped_eval_allowed` must keep refusing while the V9 gap remains. | **Open** — must not be locked yet. |
| `RQ-PROD-2` | Retrieval Quality | Production | Documented, measured behavior for the V9-gap window (passport anti-join, not `drain_status`). | Historical unpassported turns vs this-drain C–F are two facts. | **Documented**; gap **not closed**. Closing the gap is backfill work, not a formula merge. |
| `RQ-PROD-3` | Retrieval Quality | Production | Measured (not hedged) tokenizer accuracy for at least the dominant Hermes profile. | tiktoken `cl100k_base` is used for budget *counting*; that is not a measured match to Hermes’ tokenizer. | **Open** |
| `OC-MVP-1` | Observability & Control | MVP | Pane is **read-only** and renders live `drain_status` aggregates, `LEDGER.snapshot()` / `LEDGER.counts()`, current `beam_score` inputs/outputs, and `hnsw.ef_search` / budget usage **directly from source**. No independent “degraded” or score formula. No stale field names. | A drifted pane is a false picture that looks authoritative. | **Met** (honesty). Installed path `:5452`. `GET /api/health` + `/api/librarian/graph/stats` + `/api/librarian/pane` 200. CI green at `8c86181` [34181809245](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34181809245). Field-audit artifact: [`docs/reports/pane-level6-2026-09-05.md`](docs/reports/pane-level6-2026-09-05.md). Does **not** close `OC-MVP-3` or `OC-MVP-4`. |
| `OC-MVP-2` | Observability & Control | MVP | Every pane-rendered value is traced to one real field (audit table). Integrity / “healthy” / “drifted” must not mean `GRAPH_DEGRADED` or V9-gap. | Same split-brain class, one layer up. | **Met** for status/health/stats checked on `:5452` (`drain_status` buckets, `unpassported_count=0`). `verify_readonly` still reuses ingest `integrity.healthy` — named leftover, not a Met-breaker. CI green at `8c86181`. |
| `OC-MVP-3` | Observability & Control | MVP | **Given** a session with ≥3 turns mentioning ≥2 named entities each, **when** that session is opened in the pane’s live graph view, **then**: (a) at least one node per mentioned entity, labeled from `noun.label`; (b) at least one edge between two of them, and hover/click shows real source `magnitude` / `beam_score`; (c) layout is not the Session/Turn flower; (d) a human can say what the conversation was about from the graph alone. | Honesty is not meaning. | **Open.** Evidence attached, not a Met: Nightingale session on `:5452` (4 turns, 9 nouns, 7 mentions; search returned conv 5–8). That is level-5 API proof the graph is populated, **not** a [`DEFINITION_OF_DONE.md`](DEFINITION_OF_DONE.md) level-6 graph-view artifact for (a)–(d). Hover/click in (b) is `OC-MVP-4`. [#72](https://github.com/rubyrayjuntos/hermes-memory/issues/72) can hide an entity (Atlas on turn 5). |
| `OC-MVP-4` | Observability & Control | MVP | Pane implements [`docs/PANE_INTERACTION_SPEC.md`](docs/PANE_INTERACTION_SPEC.md) **§§1–3**: hover identity (no invented score), click 1-hop with real neighbors / provenance / explicit empty reasons, search funnel always visible, seed vs expanded distinction. Spec §5 dual-space (vectors never create edges) is already a CORRECTIONS fact; inferred-edge UI is deferred with that experiment. | A pane that does not lie but does nothing on click cannot be how an operator trusts the data. Multiple real bugs were caught only in the UI. | **Open — MVP gate.** Spec is binding. Fountain today is honesty + catalog/search JSON, not the specified hover/click/funnel. Prototypes (`docs/pane-prototype.html`, `docs/hermes_librarian_traversal_expansion_simulator.html`) are not the live pane. |
| `OC-PROD-1` | Observability & Control | Production | Authenticated access to the pane when it is not loopback-only. | Bound to `D-PROD-3`. Loopback bind is Alpha, not auth. | **Open** |
| `OC-PROD-2` | Observability & Control | Production | Control actions (backfill trigger, golden-set rerun, migration status/apply) require explicit confirmation, are authorized, and write their own audit trail. Mutating routes stay 501 until this row. | `visual-pane.md` CRUD / Cypher studio / re-embed. Unauthenticated writes would reintroduce silent failure. | **Open** — `match_route` already 501s POST/PATCH/DELETE. Keep it that way until this row. |

---

## Installed-path evidence (2026-09-07) — `:5452` / `hermes_memory_installed`

Pin `8c3e0de` (`~/.hermes/plugins/hybrid-age/.hermes-memory-version`, `ref=origin/main`, `installed_at=2026-09-07T21:15:53Z`). Fresh install, not an editable clone. `pip show` → site-packages. Pane on `127.0.0.1:7890`.

| Check | Result |
|-------|--------|
| Conversations on `:5452` | 4, all `drain_status='complete'` |
| Pane `GET /api/health` | 200, `ok`, `drain_status.complete=4`, `unpassported_count=0` |
| Pane `GET /api/librarian/graph/stats` | 200, `manifold.turns=4` `nouns=9` `mentions=7` (not `:5450`) |
| Pane `GET /api/librarian/pane` | 200, Fountain shell |
| Pane `GET /api/librarian/search?q=Nightingale` | 200, conv 5–8 (install-proof + follow-up) |
| File ingest on this DB | `doc_chunks=0`, `memory_entries=0` — tonight’s “ingest” was live turns, not librarian files |
| Dead tables | `sessions`/`messages`/`librarian_chunks` exist, 0 rows — prune deferred |

CI on docs commit `8c86181`: [34181809245](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34181809245) green. Pin `8c3e0de`: [34162339003](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34162339003) green. Fail-loud merge `207e635`: [34187637800](https://github.com/rubyrayjuntos/hermes-memory/actions/runs/34187637800) green.

**MVP cut is not Met.** Open MVP rows: `OC-MVP-3` (meaning bar, needs level-6 graph artifact), `OC-MVP-4` (pane interaction spec §§1–3). [#73](https://github.com/rubyrayjuntos/hermes-memory/issues/73) is naming only — does not gate any row.

---

## Pane audit (2026-09-05) — evidence for `OC-MVP-1` / `OC-MVP-2`

Honesty only. Does **not** close `OC-MVP-3` (meaning / human-readable session graph).

Opened `docs/graph/fountain.html`, `graph_http.py`, `graph_runtime.py`,
`graph_view.pack_search`, `provider_helpers.format_injection`. Not a redesign.

| What the human sees | What it actually reads | Real field today | Verdict |
|---------------------|------------------------|------------------|---------|
| Cockpit hint | `LEVEL_DEF[4]` | Walker `beam_score`; missing score is unavailable | **Updated on `9428da0`** |
| Mentions edge tooltip `score=` | `l.score` if present, else `score=unavailable` | Walker slot 6; no local mixer | **Updated on `9428da0`** |
| Injection debug `w=` `c=` `decay=` `score=` | `/api/librarian/search` paths + `retrieval.hnsw_ef_search` | `ef_search` is live from `clamp_hnsw_ef_search(store)`. `score` is walker score when present. Pane does not show `sim` / `prov_boost` / `magnitude` | **Partial** — ANN knob live; score decomposition is old mixer language |
| Prompt-debug sibling in Python | `format_injection` has no mixer. `format_debug_injection` prints `score={bs}` only. JoinedStr scan in `test_score_contract`. | Case **1** label deleted on `9428da0`. | **In tree; CI not yet for OC rows** |
| VECTOR metric caption `composite × (magnitude / 8)` | Caption only; value is `stats.vector.chunks` = `count(memory_entries)` | Caption matches `beam_score` last step; count is not ANN quality | **Caption closer than tooltip** |
| Status badge | Fountain `GET /api/health` | `LEDGER` + `drain_status` + `unpassported_count` | **Wired on `9428da0`** — not ingest `isolated_files` |
| `verify_readonly` PASS/FAIL | Reuses `integrity.healthy` | Same file-hash fact. Does **not** run `hermes-memory-verify` | **Name collision leftover** |
| Catalog / ghost / search | GET `/graph/3d`, `/graph/stats`, `/graph/ghost`, `/search` | Live graph + ANN path | **Mechanism view works**; scoring overlay does not |
| Control (backfill, migrate, CRUD) | POST/PATCH/DELETE → `501 read-only viz API` | Intended Alpha | **Correct for MVP** — do not “complete” `visual-pane.md` Issues C–D as MVP |

`CORS` allows GET (+ OPTIONS). Handler methods for POST exist only to 501.
That is the right MVP shape.

---

## How a future session uses this

1. Classify the change: **Delivery**, **Retrieval Quality**, or **Observability & Control** (derived — then say which source field the pane must read).
2. Classify the bar: **MVP** or **Production**. View vs control: control is `OC-PROD-*`.
3. In the PR / claims table / review, write `closes D-MVP-3` or `RQ-PROD-1, out of MVP scope` or `OC-MVP-1 (read path only)`.
4. Keep **this session** vs **origin/main CI** as separate evidence columns when claiming a row is Met.
5. Packaging, install docs, and out-of-scope UI chrome still go to `AGENTS.md` out-of-scope — not a fourth track. The pane is in-scope only as the seam that *displays* Delivery and RQ facts.

### Claims-table template (keep)

| Claim | This session | origin/main CI |
|-------|--------------|----------------|
| … | ran / not run | green / red / not in suite |

A local pytest pass is not a repository claim. A green PR is not a green `main`.

---

## Explicit non-goals for MVP

- Human-judged live-shaped eval as a merge blocker.
- Rolling-deploy / multi-tenant / RLS.
- Durable process ledger across restart (product change; ledger stays process-local).
- Persisting L1 failures as rows (contradicts accepted-loss policy).
- A fifth `counts()` key or `READ_FAILED` Kind for prefetch (bad read ≠ bad write).
- Treating `docs/plans/board.md` “production-ready” as current.
- Pane control actions, Cypher studio writes, or re-embed triggers (`visual-pane.md` Issues C–D).
- Letting the pane (or `format_injection`) recompute a score instead of showing `beam_score` output.
- Treating ingest `integrity.healthy` as write-path `graph_degraded` or as `hermes-memory-verify`.
- Treating `OC-MVP-1`/`OC-MVP-2` (pane does not lie) as `OC-MVP-3` (a human can read the session from the graph). A non-null `/graph/3d` body is not (d).
- Treating `OC-MVP-1`/`OC-MVP-2` as `OC-MVP-4`. Honesty is not hover/click/funnel. The interaction spec is an MVP gate of its own.
- Promoting [#72](https://github.com/rubyrayjuntos/hermes-memory/issues/72) into an MVP Delivery or OC ship-blocker. It is RQ construction, named on `RQ-MVP-1`.
- [#73](https://github.com/rubyrayjuntos/hermes-memory/issues/73) as an MVP row. Naming/hygiene only.
