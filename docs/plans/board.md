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

## In Review

_(none)_

## Verified

_(none)_

## Done

_(none)_

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
