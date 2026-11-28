# LongMemEval-S full run (500 questions, 4 conditions)

**Status at this commit: IN PROGRESS.** Ingest and the tiered conditions
are running in the background (ingest is the long pole, ~20h projected);
`no_memory` is complete (500/500); `full_context` stopped at its $12
spend cap with 35/50 of its fixed subset done. This file documents the
methodology and the partial numbers available *as of this commit* --
every number below cites the file it came from, and anything not yet
measured says so explicitly. A follow-up commit on this branch will
carry the completed run.

## Pin / provenance (exact config: `config.json`)

- hermes-memory: **v1.0.1** (`98cd4b5`) -- includes the #123 noun-lookup
  scoping fix and the V24 migration, merged via PR #128.
- LongMemEval: official repo at commit `9e0b455f` (vendored single-commit
  snapshot). Dataset `longmemeval_s_cleaned.json`,
  sha256 `d6f21ea9d6...8e9c78`.
- Judge: `src/evaluation/evaluate_qa.py`'s `get_anscheck_prompt`,
  sha256 `ecce9c4c79...4d95251`, **verified byte-for-byte identical at
  runtime** (not just source-diffed) against the harness's local copy
  before any grading ran -- see `scripts/lme_judge.py`'s docstring and
  the verification done inline in that development session. Routed
  through OpenRouter (`openai/gpt-4o-2024-08-06`) instead of a direct
  OpenAI client; the prompt text and grading logic are unmodified.
- Answer model: `anthropic/claude-sonnet-5` via OpenRouter, temperature
  0.0, 500 max output tokens/question (recorded per-question in every
  trace row).
- Eval database: fresh `hermes_memory_eval_longmemeval_500`, built from
  `scripts/migrate.py` against v1.0.1 (no `--reuse-dsn`). Never touches
  the installed database or port 5450.

## Four conditions

| Condition | Scope | Spend cap | Status |
|---|---|---|---|
| `no_memory` | all 500 | shared $15 (with tiered_off/on) | **complete**, 500/500, $1.13 |
| `tiered_off` | all 500, gated on ingest | shared $15 | in progress, gated on ingest (18/500 ingested at this commit) |
| `tiered_on` | all 500, gated on ingest | shared $15 | in progress, same gating |
| `full_context` | fixed stratified 50-question subset (seed `20261128`, `lme_fullcontext_50_subset.json`) | $12 | **stopped cleanly at cap**, 35/50, $11.81 |

`full_context` is explicitly **not** run on the full 500 per budget
instruction -- it is reported only against the other 3 conditions on the
*same* subset, never extrapolated to a 500-question number.

## Cross-question leak test (ran before any generation)

`prefetch()`'d question N's query under question M's agent_identity for
all 6 off-diagonal pairs among the first 3 ingested questions. **0/6
leaked** -- confirmed via real injected content (2.5-3.9KB per call, not
an empty/degenerate pass) with zero fragments from the wrong question's
haystack appearing. Full results: `lme_leak_test_results.json`.

## Partial numbers at this commit (source: `lme_judged/*.jsonl`, `lme_traces/*.jsonl`)

**Judge accuracy** (overall and by category):

| Condition | n | Overall | By category |
|---|---|---|---|
| no_memory | 500 | 0.094 | single-session-user 0.086 (70), multi-session 0.090 (133), single-session-preference 0.033 (30), temporal-reasoning 0.045 (133), knowledge-update 0.077 (78), single-session-assistant 0.286 (56) |
| full_context (35/50 subset) | 35 | 0.800 | knowledge-update 0.857 (7), single-session-preference 0.333 (3), multi-session 0.636 (11), single-session-assistant 1.000 (6), single-session-user 1.000 (7), temporal-reasoning 1.000 (1) |
| tiered_off | 18 | 0.833 | single-session-user 0.833 (18) -- **only one category ingested so far, not representative** |
| tiered_on | 18 | 0.889 | single-session-user 0.889 (18) -- same caveat |

**Abstention accuracy (`_abs` questions): not run** -- none of the `_abs`
question_ids have landed in any condition's completed set yet at this
commit.

**Retrieval recall** (tiered conditions only, source `lme_traces/*.jsonl`):

| Condition | n | Session recall (mean) | Turn recall (mean) |
|---|---|---|---|
| tiered_off | 18 | 1.000 | 0.944 |
| tiered_on | 18 | 0.944 | 0.944 |

**Injected tokens/question** (estimate: `len(injected_text)/4`, not a
billed metric so an exact tokenizer wasn't required):
tiered_off mean 880, tiered_on mean 866. **p95: not yet meaningful at
n=18**, will be recomputed over the full 500.

**Lookup latency** (wall time of the `prefetch()` call itself):
tiered_off p50 186ms / mean 213ms; tiered_on p50 160ms / mean 196ms.
**p95: not yet meaningful at n=18.**

**Ingest time per turn**: see the separate growth-check artifacts
(`docs/evals/longmemeval/` top-level, not this directory) -- mean
301.6ms/turn steady-state, confirmed flat as the table grows. This run's
own ingest log additionally confirms the same order of magnitude
(~450-580ms/turn per question, consistent with the growth check).

**Evidence turns dropped by noise filter, #118/#119 events**: `#118`
(`age_str()` `$`-escape bug) fires repeatedly during ingest, exactly as
documented -- logged, non-fatal, conversation rows still land (confirmed:
graph vertex MERGE fails, turn row is unaffected). `#119`
(QueueFull silent drop) did **not** fire in this run -- the harness
paces enqueue against queue depth specifically to avoid it (see
`lme_full_ingest.py`'s `queue_full_guard`). Exact noise-filter-dropped
count for evidence turns specifically: **not yet computed** (requires
the full ingest to finish before a stable has_answer-turn accounting is
meaningful); will be reported in the completion commit.

## Known weak spots (named honestly, not buried)

- `tiered_off`/`tiered_on`'s 18 judged questions so far are **all
  single-session-user** -- the dataset isn't shuffled by category in its
  stored order, so early ingest progress happens to be monotype. The
  per-category breakdown for these two conditions is **not yet
  informative** and must not be read as a real result until the run
  covers all 6 categories.
- `full_context`'s real per-question cost (~$0.337, from actual OpenRouter
  usage) ran noticeably higher than the harness's own pre-run token-count
  estimate (~$0.21/question, from a `tiktoken o200k_base` proxy) --
  Claude's own tokenizer and the fuller prompt-template overhead (session
  headers, role labels) weren't captured by that proxy. The real number is
  what's reported here; the estimate was only ever a pre-run budgeting
  aid, and its gap from reality is flagged rather than quietly corrected
  after the fact.
- A real external blocker occurred mid-run: the OpenRouter account ran out
  of credits (`402 Payment Required`) partway through both `no_memory` and
  the first `full_context` attempt, independent of either condition's own
  spend cap (both caps had plenty of headroom left). Nothing was lost --
  every completed call was already durably written to its JSONL trace
  before the failure, and both scripts resumed cleanly (skip-already-done)
  once credits were topped up.

## Reproducing

All generation/judging scripts are in `scripts/`. They assume:
- `HYBRID_AGE_DSN` / `HYBRID_AGE_DSN_LINKIBINKI` pointing at the eval
  database described above.
- `OPENROUTER_API_KEY` in the environment.
- The LongMemEval repo checked out at `9e0b455f` with
  `data/longmemeval_s_cleaned.json` present.

Run order: `lme_full_ingest.py` (ingest, resumable) →
`lme_generate.py --condition no_memory` / `--condition full_context` →
`lme_generate_tiered.py --condition tiered_off` / `--condition tiered_on`
(only processes questions the ingest checkpoint shows as landed) →
`lme_judge.py --condition <name>` for each. `lme_tiered_catchup_loop.sh`
automates re-running the tiered generation+judging every 15 minutes as
ingest progresses, so the two don't need to be driven by hand for the
full ~20h ingest window.
