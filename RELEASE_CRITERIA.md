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

Status column is a **snapshot against `origin/main` @ `9428da0` (2026-09-05)**.
Re-check before treating a row as closed. Execution state, not opinion.
Narrative (why a row exists) is [`docs/specs/HERMES_MEMORY_SPEC.md`](docs/specs/HERMES_MEMORY_SPEC.md) §5;
**Open/Met lives only in this table.**

| ID | Track | Bar | Criterion | Why it is a row (already surfaced) | Status @ 9428da0 |
|----|-------|-----|-----------|------------------------------------|------------------|
| `D-MVP-1` | Delivery | MVP | Writes fail loud **or** are durably marked. L0 never raises. L1 = no row + process `writes_failed` (accepted loss). L2 = row + `embed_null`. L3 = row + `drain_status='graph_degraded'`. | Silent loss was the original lie. | **Met** under the OR. L1 is still not a durable mark — do not collapse it into `drain_status`. |
| `D-MVP-2` | Delivery | MVP | Boot refuses to start on schema drift (`apply_pending_migrations` then `Store.require_schema_head`). | Compose init is first-boot only; live migrate is a backstop. | **Met** |
| `D-MVP-3` | Delivery | MVP | CI on every merge runs the write-path tests (L1/L3 names collectable; `integration or store or idempotent` including `test_drain_status`). | A red `main` with “36 passed” is not a green write path until the failure is opened. | **Met** on `d7fbb5f` (run 33988783433); still the write-path gate on `9428da0`. `f154df5` was **merged-but-red** on `expand_mentions` audit — historical, named. |
| `D-MVP-4` | Delivery | MVP | One scoring function in-tree (`beam_score`). No dead competing formulas. | Four-formula split-brain. | **Met** (also `RQ-MVP-1`) |
| `D-PROD-1` | Delivery | Production | Fleet-safe: revisit `DEPLOY_TOPOLOGY.rolling_deploy` vs `head_check=expected_not_applied_only`. Topology today is one provider + optional pane, not a rolling fleet. | Flipping one without the other is a known foot-gun (`AGENTS.md`). | **Open** — current topology is loopback / single-node by design. |
| `D-PROD-2` | Delivery | Production | CD runs migrations against live, not boot-time backstop only. | GitHub CD does not migrate live. | **Open** |
| `D-PROD-3` | Delivery | Production | Auth on the ports that are not loopback-only; secrets handling remains file-local (no `.env` in git). | Alpha binds `127.0.0.1:5450` / `:7890`. Loopback is not an auth story. | **Open** as Production. Loopback bind is an Alpha control, not this row. |
| `D-PROD-4` | Delivery | Production | Versioned release tags whose changelog matches `pyproject.toml`. | Tag `v0.1.0` (2026-08-24); `main` is ahead; package still `0.1.0`. | **Open** — changelog exists; version/tag have not caught `main`. |
| `RQ-MVP-1` | Retrieval Quality | MVP | One named, documented scoring function (`beam_score` in walk / architecture / manifold spec). | Same split-brain as `D-MVP-4`. | **Met** |
| `RQ-MVP-2` | Retrieval Quality | MVP | Synthetic ANN / verify path passes in CI (`hermes-memory-verify`, walk/score tests). This proves the *pipe*, not that recall is good. | Needed so Delivery hardening cannot ship a broken retriever unnoticed. | **Met** as mechanism smoke. **Not** a quality floor. |
| `RQ-PROD-1` | Retrieval Quality | Production | Live-shaped golden set with human-judged expected memories, a defined bar, enforced in CI. | Spec Injection-Hit ≥ 0.85 is advisory until that set exists (≥50 was the sprint gate). `assert_live_shaped_eval_allowed` must keep refusing while the V9 gap remains. | **Open** — must not be locked yet. |
| `RQ-PROD-2` | Retrieval Quality | Production | Documented, measured behavior for the V9-gap window (passport anti-join, not `drain_status`). | Historical unpassported turns vs this-drain C–F are two facts. | **Documented**; gap **not closed**. Closing the gap is backfill work, not a formula merge. |
| `RQ-PROD-3` | Retrieval Quality | Production | Measured (not hedged) tokenizer accuracy for at least the dominant Hermes profile. | tiktoken `cl100k_base` is used for budget *counting*; that is not a measured match to Hermes’ tokenizer. | **Open** |
| `OC-MVP-1` | Observability & Control | MVP | Pane is **read-only** and renders live `drain_status` aggregates, `LEDGER.snapshot()` / `LEDGER.counts()`, current `beam_score` inputs/outputs, and `hnsw.ef_search` / budget usage **directly from source**. No independent “degraded” or score formula. No stale field names. | A drifted pane is a false picture that looks authoritative. | **Level 5/6 this session** — see [`docs/reports/pane-level6-2026-09-05.md`](docs/reports/pane-level6-2026-09-05.md). **Not Met** until CI at `9428da0` is opened and green. |
| `OC-MVP-2` | Observability & Control | MVP | Every pane-rendered value is traced to one real field (audit table). Integrity / “healthy” / “drifted” must not mean `GRAPH_DEGRADED` or V9-gap. | Same split-brain class, one layer up. | **In tree on `9428da0`** for the status line (`live · ledger… · drain_status… · unpassported…`). `verify_readonly` still reuses ingest hygiene as PASS/FAIL — named leftover. **Not Met** until CI is opened. |
| `OC-MVP-3` | Observability & Control | MVP | **Given** a session with ≥3 turns mentioning ≥2 named entities each, **when** that session is opened in the pane’s live graph view, **then**: (a) at least one node per mentioned entity, labeled from `noun.label` — not a placeholder or id; (b) at least one edge between two of them, and hover/click shows real source `magnitude` / `beam_score` — not `undefined` or a guessed number; (c) layout is not the Session/Turn flower — node positions must differ from a prior unrelated session (driven by this session’s relationships, not a fixed shape); (d) a human can say, without being told, roughly what the conversation was about from the graph alone. (d) is not automatable. A non-null query is not a pass. | Honesty (`OC-MVP-1`/`OC-MVP-2`) says the pane must not lie. This row says a populated pane must convey the session. Same class of gap as a plausible “the pane is fixed” with no pass bar. | **Open.** Written bar only. No demonstration; [`DEFINITION_OF_DONE.md`](DEFINITION_OF_DONE.md) level 6 + artifact required. Do not credit `OC-MVP-1`/`OC-MVP-2` in-tree work as this row. |
| `OC-PROD-1` | Observability & Control | Production | Authenticated access to the pane when it is not loopback-only. | Bound to `D-PROD-3`. Loopback bind is Alpha, not auth. | **Open** |
| `OC-PROD-2` | Observability & Control | Production | Control actions (backfill trigger, golden-set rerun, migration status/apply) require explicit confirmation, are authorized, and write their own audit trail. Mutating routes stay 501 until this row. | `visual-pane.md` CRUD / Cypher studio / re-embed. Unauthenticated writes would reintroduce silent failure. | **Open** — `match_route` already 501s POST/PATCH/DELETE. Keep it that way until this row. |

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
