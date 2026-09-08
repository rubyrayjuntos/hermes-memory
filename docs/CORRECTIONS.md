# Corrections — verified wrong, do not restate

Created: 2026-09-07 · Last modified: 2026-09-07

Every row here was stated as fact at some point in this project's history and
then disproven by opening the actual file, running the actual query, or
tracing the actual call site. **A future session — human or agent — that
states the "wrong" column as if it were true is repeating an error already
paid for once.** Cite this file instead of re-deriving the correction.

Format: the wrong claim, the actual fact, where it was verified. New rows
only get added here after direct verification (a cited file:line, a query
result, a passing test) — never from a hunch.

| # | Wrong claim | Actual fact | Verified |
|---|---|---|---|
| 1 | The AGE flower (`:Turn`/`:Session`/`NEXT`/`IN_SESSION`) encodes or represents semantic/meaning relationships. | The flower is a prompt/view organizer only. `beam_score` and all retrieval logic read Postgres `noun`/`semantic_edge`, never AGE. The flower's shape is fixed by turn/session order, not content — it cannot visually encode meaning. | `docs/reports/2026-09-07-schema-function-map.md`; storage diagram, this thread |
| 2 | `beam_score`'s centrality term `c` is computed via Personalized PageRank (PPR). | `c = src_align * tgt_align` — cosine pole alignment. No PPR anywhere in the codebase. | `walk.py:76-91` |
| 3 | AGE edge label `Mentions` and SQL `semantic_edge.verb_type = 'mentions'` are the same relationship. | Two unrelated pieces of schema. Recall uses only the SQL verb. The AGE `Mentions` elabel is declared in V1 DDL with zero `src/` MATCH/MERGE calls. | `V1__vector_age.sql`; repo-wide grep, this thread |
| 4 | `_HIGH_RELEVANCE = 0.65` is a score-based inclusion/drop gate — memories below it get excluded from injection. | It only selects the header word ("high relevance" vs "related"). It never excludes a memory from `<PAST_CONTEXT>`. | `provider_helpers.py:12,108,117,214,249` |
| 5 | `isolated_files` / `orphan_deps` in the pane are real health signals, equivalent to or correlated with `GRAPH_DEGRADED`. | Both are hardcoded to `0` with no `SELECT` behind them. Cosmetic, currently inert, unrelated to write-path health. Confirmed prune candidates. | `graph_runtime.py:210,263,267` |
| 6 | The epistemic-notice pattern (`<epistemic_notice>`) is implemented, or "PARTIAL," on `main`. | Not implemented at all as of the SHAs checked. Zero function found by grep. Proposal only — destination-PDF code, never merged. | Repo-wide grep, multiple sessions this thread |
| 7 | The system does true bi-temporal modeling (two independent clocks: valid-time and transaction-time). | At most, single-clock valid-time supersession is proposed/landing. Transaction-time (`T_i`) is not implemented anywhere. Do not call the store "bi-temporal" until both clocks exist. | `HERMES_MEMORY_SPEC.md` §4; destination PDF review |
| 8 | `recency_score` / `legacy_composite` (`0.5·c + 0.3·w + 0.2·decay`) is a live or fallback scoring path. | Deleted. `beam_score` is the only weighted-sum scorer in the tree. Enforced by an AST test that fails CI on any new 3-term weighted sum outside `walk.py`/`store.py` — not a literal-coefficient grep, which a rewritten constant could dodge. | `test_score_contract.py:126` |
| 9 | Mention chains form a fully connected co-mention graph — every noun in a turn links to every other noun. | Adjacent pairs only. A path of consecutive extracted nouns, not a mesh. See also: `MAX_NOUNS` cap can silently drop a real entity before it ever gets a chance to link. | Issue #72; `provider.py:580-581`, `extract_nouns.py:100` |
| 10 | The installed Hermes plugin is automatically in sync with GitHub `main`, or with whatever's in a developer's local clone. | Never assume this. The plugin install is a separate, pinned, versioned artifact. A dev clone being correct on GitHub proves nothing about what Hermes is actually running until the install itself is independently verified (pin SHA, version stamp, file list). This exact assumption cost real time. | This thread's install-drift investigation |
| 11 | "Pre-Compress Checkpoint API V2" (a fail-closed `on_pre_compress` with a version flag) is a real, existing Hermes host capability this plugin uses. | The real Hermes hook is a plain `on_pre_compress(messages)`, no version flag, not fail-closed by default. This plugin does not opt into any v2 contract — confirmed absent from `provider.py` by direct grep. | `provider.py` grep, this thread; Hermes host docs |
| 12 | `hermes-memory-backfill` performs the live C–F drain (nouns/mentions/passports) for historical turns. | It only writes AGE `Concept`/`ABOUT` edges via the legacy linker. It does not run `extract_nouns`, passports, `mentions`, or stamp `drain_status`. The actual C–F backfill tool is `scripts/replay_conversation_manifold.py --live`. | Issue #73; `backfill.py:100`, `replay_conversation_manifold.py:276-297` |
| 13 | A dev clone's local `.env` (or absence of one) safely inherits or defaults to the correct database. | `load_config()` silently falls back to `:5450`/`hermes_memory` if the named DSN env var is unset. This is a live, filed risk (#74) — a process that should have a DSN and doesn't will guess wrong, silently. | `config.py:27-90`; issue #74 |

**Rule for adding to this file:** state the wrong claim exactly as it would plausibly be said, the corrected fact, and a citation someone could open and check today. If you can't cite it, it doesn't go in this file yet — verify first.
