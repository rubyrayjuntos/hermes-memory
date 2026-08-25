# Kanban Board — hermes-memory v0.1

Living board. Move cards by editing their section; card spec source of truth:
[`docs/plans/v0.1.md`](v0.1.md) §5 (acceptance criteria) + §3–4 (interface contracts).

**Columns:** Backlog · Claimed · In Review · Verified · Done
**Rules:** one `wip/<card-id>` branch per card · two-stage review before Verified ·
branch deleted at merge · contracts (§3/§4) frozen — change proposals go to controller.

---

## Backlog

_(empty — all cards spec'd)_

## Claimed

- [ ] **C1 — Scaffold & consolidation** *(ready — no deps)*
- [ ] **C2 — Docker + schema + indexes** *(ready — no deps, parallel with C1)*

## Done

- [x] **C8 — Synthetic load generator** — spec PASS (incl. confirming + fixing C7's silent agtype parser bug), quality APPROVED after fix cycle (`1a8a99f`: batch isolation, streaming corpus ~10× RAM reduction, stable savepoint hashes). 1000 rows in 5.7s; expansion proven firing via --debug-graph. C7 report amended with correction notice.
- [x] **C7 — Benchmark harness** — spec PASS, quality fixes applied (`df4e077` evidence doc + cleanup). First real numbers: Injection-Hit 0.90, throughput 16 turns/sec. Honest finding: bridge table empty → graph expansion contributed nothing yet (extractors v0.2); ablation re-run planned post-C8/C10. Report: docs/reports/c7-bench.md.
- [x] **C6 — Docs truth-pass** — spec PASS (cold-walk verified), nit fixed (94d0a8f).
- [x] **C5 — Verify harness & tests** — spec PASS, security regression check PASS, quality APPROVED after fix cycle.
- [x] **C3 — Provider port** — spec PASS (every §3.1 row runtime-verified; warm prefetch 88ms), quality APPROVED after fix cycle (`6e8da18`: idempotent init, drain-task strong ref, Cypher key validation, shutdown ordering, fire-and-forget enqueue, LIKE escaping, batch padding). Live smoke: rows land both tables, DB-down degrades to "".
- [x] **C2 — Docker + schema + indexes** — spec PASS (live end-to-end verified), quality fixes applied & verified (`be7c82d`). Image pinned `apache/age:release_PG17_1.6.0` (1.7.0-for-PG17 does not exist; Appendix A). GIN containment index proven with Bitmap Index Scan on 5k rows.
- [x] **C1 — Scaffold & consolidation** — MERGED (62844ac). Spec PASS, quality APPROVED after fix cycle.

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

Property tests P1–P8 land with C3/C4/C5 per register in v0.1.md §6.
