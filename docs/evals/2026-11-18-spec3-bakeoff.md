# Spec 3 T3.6 bake-off — 2026-11-18

Throwaway Postgres: `hermes-memory-postgres:local` as `hermes-spec3-bakeoff-pg` on `127.0.0.1:5455`, database `hermes_spec3_eval`. Migrated V1–V22. Port 5450 untouched. Installed `:5452` not written. `extract_mode` default remains `rules_only`.

Runner: `scripts/spec3_bakeoff.py` with `PYTHONPATH=<worktree>/src` and `.venv-replay`. Keys from environment only (`OPENROUTER_API_KEY` via `~/.hermes/.env`; Ollama dummy `HYBRID_AGE_EXTRACT_KEY=ollama`). No keys in this file.

`origin/main` at merge of #103: `0df4b44`.

## Candidates

| Label | Endpoint | Model id | Ran? |
|---|---|---|---|
| rules_only baseline | — | — | yes |
| llama3.2:3b | `http://127.0.0.1:11434/v1` | `llama3.2:3b` (local, 2.0 GB on this host) | yes, `json_object` |
| Nemotron 3.5 Lightning | `https://openrouter.ai/api/v1` | `nvidia/nemotron-3.5-lightning:free` | yes, `json_object` |
| Longcat 2.5 | Nous `https://inference-api.nousresearch.com/v1` `meituan/longcat-2.5:free` | yes, `json_object` |
| Step 3.7 Flash | Nous `https://inference-api.nousresearch.com/v1` `stepfun/step-3.7-flash:free` | **partial** — golden yes, paraphrase hit 429 rate limit after 3 retries |

`json_schema` mode was not run in this pass (still default `json_object`). Per-call p50/p95 Unmeasurable: extractor wrap raced the worker; wall_ms is the ingest+drain clock.

## Golden v2 (ideal set n=11)

| Run | P | R | F1 | pasted FG | junk | model jobs | failed | 429 | timeout | schema-invalid | wall_ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rules_only | 1.0 | 1.0 | 1.0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3772.4 |
| llama3.2:3b json_object | 1.0 | 1.0 | 1.0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 7130.6 |
| nemotron-lightning:free json_object | 0.917 | 1.0 | 0.957 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 22733.1 |
| **longcat-2.5:free json_object** | **1.0** | **1.0** | **1.0** | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 305488.8 |
| **step-3.7-flash:free json_object** | **0.917** | **1.0** | **0.957** | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 25393.2 |

Nemotron extra claim (precision drop): `Austin is the office city`.
Step 3.7 Flash extra claim: Same as Nemotron.

## Paraphrase slice (ideal n=3; rules cannot match)

| Run | P | R | F1 | actual | pasted FG | junk | abstain FP | model jobs | failed | 429 | wall_ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rules_only | — | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3686.7 |
| llama3.2:3b json_object | — | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 14525.2 |
| nemotron-lightning:free json_object | 0.0 | 0.0 | 0.0 | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 21805.5 |
| **longcat-2.5:free json_object** | **0.0** | **0.0** | **0.0** | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 11252.0 |
| **step-3.7-flash:free json_object** | — | — | — | **429 rate limit** | — | — | — | 1 | 3+ | 3+ | timeout |

Nemotron slice output (not an ideal match): `I role_is staff engineering` vs ideal `staff engineer`. Model jobs=1 because only `these days I'm doing staff engineering` misses self-templates; the other paraphrases have no first-person cue so they do not enqueue.
Longcat 2.5 slice output: Same as Nemotron (`role_is staff engineering`).
Step 3.7 Flash: Hit fair-share rate limit on retry_after=106s, then 98s, then 66s. Free tier exhausted.

Cost per 1k turns: Unmeasurable (no billed usage on free/local).

## Gate G3

Not decided here. `rules_then_model` stays opt-in. Hosted models stay off the plugin default.

JSON: `docs/evals/2026-11-18-spec3-*.json`.
