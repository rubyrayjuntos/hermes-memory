# Definition of Done — evidence levels

This file is the **gate**, not a third status table.

- [`docs/specs/HERMES_MEMORY_SPEC.md`](docs/specs/HERMES_MEMORY_SPEC.md) — what the machine is and why
- [`RELEASE_CRITERIA.md`](RELEASE_CRITERIA.md) — the only Open/Met cells
- **This file** — how far a claim is allowed to travel before it may enter either of those as Met / NOW

A row in `RELEASE_CRITERIA.md` may flip to Met only when this file’s required
level is met **and** the artifact is attached (PR, issue, or the claims table
in that file). “Closes `D-MVP-3`” is not a substitute for “I opened it and it
did the thing.”

**Require the artifact, not the assertion.** “I started it and it works” is
still a claim. A curl body, browser snapshot, or terminal transcript from
*this* run is evidence. Careful wording cannot satisfy that.

---

## The two axes (do not collapse)

| Axis | Question | What this thread already did well |
|------|----------|-----------------------------------|
| **True about the code** | Did someone open the file / test / CI log? | Levels 1–4 |
| **Usable when you touch it** | Is the process up, and did a human get a real response? | Levels 5–6 |

Verified-in-code answers “did someone check.” Running and demonstrated answer
“does it work when you touch it.” Months of hardening with nothing listening
on `:7890` is what happens when those are treated as the same question.

---

## Levels

| Level | Name | Meaning |
|------:|------|---------|
| 1 | Written | Described in a spec or PR. |
| 2 | Coded | A commit implements it. |
| 3 | Tested | An automated test exists and passes **locally**. |
| 4 | Merged & CI-green | On `origin/main`, and the CI run **at that SHA** is green. Not “ran locally.” Same discipline as the claims table in `RELEASE_CRITERIA.md`. |
| 5 | Running | The process is up and reachable. Someone hit it (curl, browser, client) and got a **real** response — not “should return X.” Attach the output. |
| 6 | Demonstrated | A human interacted with the running thing, watched the behavior match the claim, and attached an artifact (screenshot, transcript, saved response). |

---

## Which level is required

| Kind of work | Minimum before “done” / Met |
|--------------|-----------------------------|
| **User-facing** — pane, CLI you open and use (`hermes-memory-verify`, install, prefetch you can see in a session) | **Level 6.** Artifact required. |
| **No screen** — `drain_status` after crash, AST guard, migration boot gate, SQL anti-join | **Level 4**, or **5** if the claim is about live infrastructure (real Postgres, not a fake store). Nothing to demonstrate beyond the test/curl against that infra. |

The pane specifically sat at level 4 (merged, claims file-checked) while being
talked about as if it were near 6. That is not allowed again.

---

## How a claim is recorded

Use the existing two columns, plus the level and a pointer to the artifact:

| Claim | Level reached | This session | origin/main CI | Artifact |
|-------|---------------|--------------|----------------|----------|
| … | 4 / 5 / 6 | ran / not run | green / red / not in suite | path, gist, or “none — not done” |

No artifact → not level 5 or 6 → not Met for a user-facing row.

---

## Explicit non-goals

- This file does not duplicate Open/Met (`RELEASE_CRITERIA.md`).
- This file does not redefine prefetch or scoring (spec §§2–4).
- A green unit test is not level 5.
- A compose file existing is not level 5.
- Starting the stack “if you want” after calling the work done is the failure mode this file exists to forbid.
