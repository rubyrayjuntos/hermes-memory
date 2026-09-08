# Documentation map

Start at the repo [README](../README.md) (Quick Start). Human how-tos live on the
[GitHub wiki](https://github.com/rubyrayjuntos/hermes-memory/wiki). Binding
status lives only in the files below — not on the wiki, not in `docs/plans/`.

| I want… | Go here |
|---------|---------|
| To install and seed three facts | [README Quick Start](../README.md) |
| To know what the machine *is* | [HERMES_MEMORY_SPEC.md](specs/HERMES_MEMORY_SPEC.md) §2–4 (NOW). Do not quote §6 as current `main`. |
| To know if a release row is Open or Met | [RELEASE_CRITERIA.md](../RELEASE_CRITERIA.md) **only** |
| To know what evidence “done” needs | [DEFINITION_OF_DONE.md](../DEFINITION_OF_DONE.md) (pane = level 6 + artifact) |
| To avoid restating a verified-wrong claim | [CORRECTIONS.md](CORRECTIONS.md) |
| Pane hover / click / funnel rules | [PANE_INTERACTION_SPEC.md](PANE_INTERACTION_SPEC.md) |
| The live inspector | `docs/graph/fountain.html` via `http://127.0.0.1:7890/api/librarian/pane` |
| AGE / Cypher gotchas | [age-quirks.md](age-quirks.md) |
| Agent conventions | [AGENTS.md](../AGENTS.md) |

**Two databases on purpose.** Documents clone: `:5450` / `hermes_memory`.
Installed pin: `:5452` / `hermes_memory_installed` (`hermes-memory-install`).
Missing `HYBRID_AGE_DSN` after dotenv **raises** (`load_config`).

**Live C–F vs leftover ABOUT.** Conversation nouns/passports/mentions:
`python scripts/replay_conversation_manifold.py --live`.
`hermes-memory-backfill-about` is leftover Concept/ABOUT only (old name exits 2).

## Scratch and evidence (not status)

| Path | What it is |
|------|------------|
| `docs/plans/` | Sprint scratch. `board.md` / `sprint-v02.md` “v0.2 production-ready” is **not** current. |
| `docs/reports/` | Dated evidence artifacts. A report is not a Met flip unless `RELEASE_CRITERIA.md` says so. |
| `docs/architecture.md` | **Retired** (`queue_prefetch` is not in the tree). |
| `docs/pane-prototype.html`, `docs/hermes_librarian_traversal_expansion_simulator.html`, `docs/dashboard/`, `docs/graph/3d.html` | Prototypes / legacy demos. **Not** the live Fountain pane. |
| `docs/superpowers/` | Agent session notes. |
| `docs/specs/persistent-memory-spec.md` | Historical export. Superseded by `HERMES_MEMORY_SPEC.md`. |

MVP still **Open** (do not wiki-copy as done): `OC-MVP-3` (session meaning),
`OC-MVP-4` (interaction spec §§1–3; click chrome is [#80](https://github.com/rubyrayjuntos/hermes-memory/issues/80)).
Delivery fail-loud / ingest stamps landed on `main`; honesty rows `OC-MVP-1`/`OC-MVP-2` are Met in the criteria table, not in this file.
