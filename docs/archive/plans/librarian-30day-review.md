# Librarian 30-Day Plugin Entry Review Gate

**Created:** 2026-08-27  
**Review due:** 2026-09-26 (30 days)  
**Owner:** @rubyrayjuntos  
**Context:** Umbrella skill + `librarian-health` cron run repo-local for 30 days with no Hermes core change (per 2026-08-27 advice). See `skills/librarian/SKILL.md` § "30-day plugin entry review gate".

## What runs locally for 30 days

- `skills/librarian/SKILL.md` (umbrella orchestrator, no new Hermes model tool)
- `skills/librarian/references/*.md` (health, ingestion, verification, git-hygiene)
- `scripts/librarian_health.py` + `Store.librarian_health()` (scoped to `file_path IS NOT NULL`)
- `.github/workflows/librarian-health.yml` (daily 09:00 UTC, `continue-on-error` → drift issue on `steps.health.outcome == failure`)
- `.opencode/agent/librarian.md` + `.opencode/command/librarian.md`

## Review checklist (2026-09-26)

- [ ] How many drift issues did daily health file? (expect 0; if >2, tune threshold/label)
- [ ] `gh run list --workflow=librarian-health.yml` — green rate? Any flake from pgvector service?
- [ ] Property/integration pass rate on `main` stayed green?
- [ ] Did `Store.librarian_health` scoped query prevent false positives on user prefs (ids 1799-1801 pattern)?
- [ ] Is umbrella skill used via `/librarian health|ingest|verify` at least 5×?

## Decision

- **If stable & used:** propose Hermes plugin entry:
  `project.entry-points.hermes_agent.memory_providers` or `hermes skill install ./skills/librarian` — add to `pyproject.toml` / curator manifest. Open Tracking Issue `librarian: promote to hermes plugin` with evidence.
- **If noisy/unstable/unused:** keep repo-local, delete cron or keep manual-only, close this doc with notes.

## How to close

This file is the reminder. The actual scheduled scheduler is **Hermes cron job `librarian-30day-review` (job `a2cf8ca26920`, once at 2026-09-26 09:00 UTC, `deliver:origin`)** and **GitHub Issue #21** (open, `enhancement`). No `.github/workflows/` job runs a `gh issue create` on that date — the fenced snippet below is documentation only. On 2026-09-26 the Hermes cron will deliver a nudge to the origin chat; Issues #21 already tracks the gate. Delete this file when decision is recorded, or convert to `docs/plans/librarian-promotion.md`.

---
*Scheduled reminder (already created):*
- Hermes cron: `librarian-30day-review` → 2026-09-26T09:00:00Z, deliver origin
- GitHub Issue #21: https://github.com/rubyrayjuntos/hermes-memory/issues/21
- Snippet (for manual re-run if needed):

```bash
gh issue create --title "Review: librarian 30-day plugin promotion (due 2026-09-26)" \
  --body "See docs/plans/librarian-30day-review.md — evaluate drift rate and promote or keep repo-local." \
  --label "roadmap"
```
