# Kanban Board — hermes-memory

> **Source of truth: GitHub Project #6 📜The Hermes Librarian Sprint board** — https://github.com/users/rubyrayjuntos/projects/6
> This file is **execution scratch for the kanban skill's sub-agent swarm** (one `wip/<card>` branch per card, two-stage review).
> Do not treat it as ledger — `gh project item-list 6` and Sprint Iteration view are authoritative.

**Columns (GitHub):** Todo · In Progress · Done (+ Sprint Iteration)  
**Columns (local scratch):** Backlog · Claimed · In Review · Verified · Done  
**Rules:** one `wip/<card-id>` branch per card · two-stage review before Verified · branch deleted at merge · contracts (`v0.1.md` §3/§4) frozen

---

## Status as of 2026-09-01 — v0.2 production-ready plugin (soak)

v0.1+v0.2 code shipped to `main` — Project #6 Sprint view is the ledger, this file mirrors it.
Production-ready hermes plugin achieved at `v0.2`; `C7-C10` remain in Sprint 2 for fine-tuning soak on live corpus.

## Backlog (GitHub Project Backlog — not yet queued, open issues)

- #9 Profile-scoped memory identity (Todo, Sprint 2, P2)
- #12 Agent PR identities (Todo, Sprint 2, P2)
- #14 context hermes profile bound or global? (Todo, P2)
- #21 30-day plugin promotion due 2026-09-26 (Todo, Sprint 2, P1)
- #28 CLI overhaul install/upgrade/migrate/uninstall (Todo, Sprint 2, P2)

These 5 are intentionally open — v0.2 is code-complete; they await sprint queuing for soak tuning.

## Done (Project #6 Done — mirrors local Done)

- [x] **C1 — Scaffold & consolidation** — MERGED `62844ac`
- [x] **C2 — Docker + schema + indexes** — `be7c82d` (`apache/age:release_PG17_1.6.0`)
- [x] **C3 — Provider port** — `6e8da18` (warm prefetch 88ms)
- [x] **C4 — Ingest port** — `9316629` + `318f74b` (hash/dedup/drift, drift-flag proof)
- [x] **C5 — Verify harness & tests** — property suite P1-P8
- [x] **C6 — Docs truth-pass** — `94d0a8f`
- [x] **C7 — Benchmark harness** — `df4e077` — *code DONE, re-run in Sprint 2 for fine-tuning on live 699v/1936e*
- [x] **C8 — Synthetic load generator** — `1a8a99f` — *code DONE, regen 10K/100K in Sprint 2 soak*
- Closed issues: #1, #2, #3, #4, #10, #17 (taxonomy enrichment #17 includes ids 1799-1801 fix)

## Soak / Fine-tuning (Sprint 2 — 2026-09-07, see `sprint-v02.md`)

- **C7** and **C8** — code DONE, remain `ready`-equivalent for re-ablation after soak
- **C9** Production install execution — `blocked on Phase-2 pre-flight` → unblock in Sprint 2
- **C10** Tuning experiments (weights 0.8/0.1/0.1 etc, budgets 500→9999) — `blocked on C7 + real data` → runs on accumulated corpus

---

### Card quick reference

| Card | Title | Depends | Acceptance (abbrev) |
|---|---|---|---|
| C1 | Scaffold & consolidation | — | import works; editable install; tree per plan §2 |
| C2 | Docker + schema + indexes | — | compose up healthy; GIN used on 5k-row EXPLAIN; volume persists |
| C3 | Provider port to ABC | C1 | Hermes discovers plugin; setup wizard; rows land; <2s warm prefetch |
| C4 | Ingest port | C2 | fixture ingest counts exact; re-run = 0 new; drift flagged |
| C5 | Verify harness + tests | C3,C4 | property suite green; verify.py catches broken drain; CI green |
| C6 | Docs truth-pass | C5 | cold-start README walkthrough <10 min; claims match artifacts |
| C7 | Benchmark harness | — | ablation table + 4 metric families (soak re-run) |
| C8 | Synthetic load generator | — | 10K corpus <5 min, teardown clean (soak regen) |
| C9 | Production install | Phase-2 | V1-V5 gates, benchmarks as V-gate |
| C10 | Tuning experiments | C7+data | chosen profile + budget recorded in config+README |

Property tests P1–P8 land with C3/C4/C5 per register in v0.1.md §6.
Sub-agent flow now mirrors GitHub: `gh project item-edit` updates Status/Sprint, not just this file.
