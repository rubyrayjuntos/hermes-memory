# T3.6 follow-up: model-enqueue bug fix + batch-size / reasoning-token diagnostics

## Bug found and fixed

`should_enqueue_model` (extraction_jobs.py) had a fallback branch:

```python
if not claims:
    return not self_claims   # not [] == True -- enqueues almost anything
```

Any authored turn with zero rule-extracted claims and zero first-person cue
was enqueued for the model by default, because `self_claims` is an empty
list in that case and `not []` is `True`. On the paraphrase slice this
pulled the pasted-resume turn's preamble ("Please summarize this pasted
resume:") into the same model batch as the three real paraphrase turns,
diluting the prompt. Fixed by falling through to the existing
noun-coverage check instead of unconditionally returning `True`. Test:
`tests/test_enqueue_model_no_cue.py` (RED before fix, GREEN after).
`MODEL_VERSION` bumped `2` -> `3` since this changes which turns reach the
model in `rules_then_model` mode.

Verified: `durable_job.payload.span_ids` went from `[1, 2, 3, 5, 6]`
(5 spans, including the pasted-doc turn) to `[1, 2, 3, 5]` (4 spans) after
the fix. Source: `docs/evals/2026-11-18-paraphrase-post-fix-llama32-3b.json`
(before) vs `docs/evals/2026-11-18-paraphrase-post-enqueue-fix-llama32-3b.json`
(after).

Separately confirmed the pasted-resume turn produces **zero claims from the
rules path too**, both runs: `pasted_span_false_grounding_count: 0` in every
file in this set, and a direct `claims` table query for "Boston" returned 0
rows. The extraction worker already restricts rules extraction to
authored-only segments (turn_filter.segment_turn), so this was never at risk.

## What the fix did and did not change

The enqueue fix is real and tested, but **did not improve llama3.2:3b's
paraphrase recall** (still P=0.0 R=0.0 after the fix — same inverted claim
on span 3, same hallucinated Denver claim from span 5's hypothetical).
Source: `docs/evals/2026-11-18-paraphrase-post-enqueue-fix-llama32-3b.json`.

## Batch-size diagnostic (MODEL_BATCH_SIZE=1, not shipped)

Ran llama3.2:3b with `MODEL_BATCH_SIZE` temporarily patched to `1` (one
`durable_job` per span instead of one job for all eligible spans in a
session). Reverted after the run; production value stays `8`.

| span | text | batch=8 result | batch=1 result |
|---|---|---|---|
| 1 | "moved back home to New York last spring" | missed | `home lives_in New York` (wrong subject) |
| 2 | "Started at Initech in March" | missed | missed |
| 3 | "these days I'm doing staff engineering" | inverted subject/object | missed |
| 5 | "if I moved to Denver..." (hypothetical) | hallucinated `Denver lives_in (self)` | correctly produced nothing |

Source: `docs/evals/2026-11-18-paraphrase-batchsize1-llama32-3b.json`.

**Finding:** batching is a real contributing factor to two specific error
modes (the span-5 hallucination and the span-3 inversion both disappeared
at batch size 1), but it does not explain llama3.2:3b's overall recall
failure -- 2 of 3 real claims were still missed even with isolated calls.
Model capability, not batching, is the dominant limiter.

**Follow-up (not implemented here):** an optional per-model
`MODEL_BATCH_SIZE` override (1 for small local models, 8 default for
larger/hosted models) is worth a separate issue — see tracking issue
opened alongside this PR.

## Nemotron 3.5 Lightning: reasoning tokens, not a parser bug

The 7942-8496 "completion token" counts seen on Nemotron runs are
**`usage.reasoning_tokens`**, a field the OpenAI-compatible SDK already
excludes from `message.content`. Captured the real raw `message.content`
from a live pipeline run via a temporary debug hook (reverted after use,
not shipped): it was `{"claims": []}` -- 37 bytes, valid JSON, parses
clean. There is no parser bug to fix; the model itself returned zero
claims after ~8000 tokens of hidden reasoning.

A second, independent isolated call with the identical 4-turn prompt and
`temperature=0` returned a *different* result: one claim
(`I role_is "staff engineering"`). Same model, same prompt, same
temperature, different output -- this points to **response
non-determinism**, most likely from OpenRouter's free-tier routing
sending the request to different backend instances. This makes Nemotron
3.5 Lightning unreliable for this use case independent of recall: ~8000
reasoning tokens and 40-60s wall time per call, with outputs that vary
between identical calls.

## Net effect on Gate G3

No model shows a verified, reproducible positive result on the paraphrase
slice. `rules_only` stays the default; no local or hosted model is
recommended yet. See source files above for every number cited in this
document.
