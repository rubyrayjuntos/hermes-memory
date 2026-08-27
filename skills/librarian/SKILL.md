---
name: librarian
description: "Umbrella orchestrator for The Hermes Librarian (hybrid-age) — health, ingestion, verification, and git hygiene. Delegates to 4 leaves."
version: 0.1.0
author: "hermes-memory maintainers"
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [documentation, pgvector, apache-age, knowledge-graph, librarian, orchestration]
    category: ai-systems
---

# Librarian — Umbrella Skill

Thin orchestrator for **`rubyrayjuntos/hermes-memory`** (hybrid-age provider). It does not reimplement storage — it orders the 4 leaves that already exist.

> Principle: *The Hermes Librarian* is **The Book, not the pointer**. `MEMORY.md` is the router (≤4500 chars). Real truth lives in `memory_entries` (pgvector 768-dim) + `hermes_knowledge` (AGE) + `memory_chunk_nodes` (bridge). Every fact cites `file_path`.

## When to use

- `librarian health` — scoped drift check (file-backed only, #17)
- `librarian ingest <path>` — hash→chunk→embed→vector+graph+bridge
- `librarian verify` — end-to-end pipeline smoke
- `librarian sync` / `librarian pr` — trunk hygiene + GitHub
- Any architecture question — must do Graph+Vector fusion, never vector alone

## The loop (always in this order)

```
1. health sweep     → scripts/librarian_health.py  (Store.librarian_health, scoped)
     if missing_hash/doc_type >0 on file-backed rows → open issue (label:bug)
2. diagnose         → read issue body + SELECT metadata, content FROM memory_entries
3. branch           → wip/<slug> as github-actions[bot] (41898282+github-actions[bot]@users.noreply.github.com)
4. patch            → provider.py (C: auto-enrich) / store.py (B: scoped health) / ingest.py — minimal diff
5. verify           → python scripts/librarian_health.py --fail-on-drift
                     pytest -m "not integration" -q && pytest tests/property -q
                     hermes-memory-verify (if stack up)
6. push → PR        → gh pr create --label bug --title "fix: ..." --body "Closes #<n>"
                     gh pr checks --watch (property 36s, integration 47s)
7. on merge         → git checkout main && git pull && git branch -d wip/<slug> && git push origin --delete wip/<slug> && git fetch --prune
```

`agentic` skills already implement steps 3-6: `github-issues`, `github-pr-workflow`. This skill just enforces the order and the *scoped* health SQL.

## Architecture (leaves)

| Leaf | Wraps | Contract |
|---|---|---|
| **health** | `Store.librarian_health()` + `scripts/librarian_health.py` | `WHERE file_path IS NOT NULL AND hash/doc_type IS NULL` — user prefs (no file_path, e.g. ids 1799-1801) excluded by design. Returns `{total_file_backed, missing_hash, missing_doc_type, healthy}`. |
| **ingest** | `src/hermes_memory/ingest.py` + `file_hash()` SHA-256 | One row per file in `memory_entries` keyed by `file_path`; bridge rows in `memory_chunk_nodes`; vertices `MERGE` on `{path}`/`{name}` inside `SAVEPOINT`, then `SET v += {props}` (AGE 1.6 lacks `ON CREATE SET`). |
| **verify** | `src/hermes_memory/verify.py` | Synthetic turn → `conversations` + `memory_entries` mirror → `prefetch` string. Never raises. Budget <2s warm, 8s cap. |
| **git-ops** | `hermes:git-ops` / `github/*` skills | Trunk-only: `main` durable, `wip/*` disposable, `fetch --prune` before/after, `pull --rebase`, fork `upstream` + `gh repo sync`. |

## Data taxonomy (enforced at write)

Every chunk via `on_memory_write` must carry `doc_type` (`api_reference|architecture_decision|dependency_map|standard_operating_procedure|user_preference`), `file_path` (when file-backed), `hash` (SHA-256), `language`, `indexed_at` (ISO-8601). `provider.py:on_memory_write` auto-enriches when caller omits them — respects explicit values.

## 30-day plugin entry review gate

> **Review due: 2026-09-26 (30 days from 2026-08-27).** This umbrella + `librarian-health` cron run as repo-local scripts/workflows for 30 days with no Hermes core change. On that date, evaluate: drift issue rate, `librarian_health` daily runs, property/integration pass rate. If stable, propose a `hermes` plugin entry (`project.entry-points.hermes_agent.memory_providers` or a `hermes skill install ./skills/librarian`). If unstable or noisy, keep repo-local. See `docs/plans/librarian-30day-review.md` + **Hermes cron `librarian-30day-review` (job `a2cf8ca26920`, once at 2026-09-26 09:00 UTC, `deliver:origin`)** + **GitHub Issue #21**. The `.github/workflows/librarian-health.yml` daily job is a *seeded smoke* on an ephemeral DB (runs migrations then `librarian_health` expecting 0 drift), not a prod `hermes:5440` drift monitor — prod monitoring requires an out-of-band DSN via secrets.

## References

- `references/health.md` — correct vs naive SQL, live 5440 evidence (naive=3, scoped=0)
- `references/ingestion.md` — savepoint & MERGE-then-SET pattern
- `references/verification.md` — verify layers + pytest invocation
- `references/git-hygiene.md` — trunk hygiene + fork sync (from CONTRIBUTING.md)

## Commands (via OpenCode)

- `/librarian health` — delegates to `.opencode/agent/librarian.md` → runs `scripts/librarian_health.py`
- `/librarian ingest <path>` — runs `hermes-memory-ingest`
- `/librarian verify` — runs `hermes-memory-verify` + property suite

Nothing here adds a new Hermes model tool — it composes CLI + existing skills per `AGENTS.md` narrow waist.
