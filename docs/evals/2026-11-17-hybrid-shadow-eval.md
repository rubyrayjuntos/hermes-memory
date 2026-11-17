# Hybrid vs vector shadow eval — 2026-11-17

Branch: `shadow-replay-v2` from `origin/main` `1fd56c03571e485b06f82bee8b82f44b68d62fbf`.
`retrieval_mode` package default was not changed (`HybridAgeConfig().retrieval_mode == vector`).
Throwaway eval Postgres: `hermes-memory-postgres:local` as `hermes-eval-pg` on `127.0.0.1:5453`, database `hermes_memory_eval`.
Installed DB: `127.0.0.1:5452/hermes_memory_installed` (SELECT on `conversations`; writes only `retrieval_shadow`).
Runner: `/home/rswan/Documents/Codex/hermes-memory/.venv-replay/bin/python`.

DSN passwords are `$HERMES_PG_PASSWORD` from `~/.hermes/.env` (not copied here).

## Commands

```bash
git fetch --prune && git switch main && git pull --ff-only && git switch -c shadow-replay-v2

docker rm -f hermes-eval-pg
docker run -d --name hermes-eval-pg \
  --env-file <scratch>/eval-pg.env \
  -p 127.0.0.1:5453:5432 \
  hermes-memory-postgres:local
# wait: docker exec hermes-eval-pg pg_isready -U hermes -d hermes_memory_eval

.venv-replay/bin/python scripts/migrate.py --dsn "$EVAL_DSN"
docker exec hermes-eval-pg psql -U hermes -d hermes_memory_eval -c "CREATE DATABASE hermes_test OWNER hermes;"
.venv-replay/bin/python scripts/migrate.py --dsn "$TEST_DSN"

.venv-replay/bin/python -m pytest -q tests/test_shadow_replay.py --migrate --dsn "$TEST_DSN" -o addopts= --tb=short

# Default-size eval (facts=200 chatter=800 queries=150) — killed
.venv-replay/bin/python scripts/eval_memory.py --dsn "$EVAL_DSN" --retrieval-mode vector

# Completing eval (after DROP/CREATE + migrate)
.venv-replay/bin/python scripts/eval_memory.py --dsn "$EVAL_DSN" --retrieval-mode vector \
  --facts 40 --chatter 80 --queries 40 --other-facts 10 --prefetch-timeout 30 --drain-timeout 300
.venv-replay/bin/python scripts/eval_memory.py --dsn "$EVAL_DSN" --retrieval-mode hybrid \
  --facts 40 --chatter 80 --queries 40 --other-facts 10 --prefetch-timeout 30 --drain-timeout 300

.venv-replay/bin/python scripts/shadow_replay.py --dsn "$EVAL_DSN" --limit 500 --json-out shadow-eval.json
docker rm -f hermes-eval-pg

.venv-replay/bin/python scripts/shadow_replay.py --dsn "$INST_DSN" --limit 500 --json-out shadow-installed.json
.venv-replay/bin/python scripts/release_gate.py
```

`$EVAL_DSN` = `postgres://hermes:$HERMES_PG_PASSWORD@127.0.0.1:5453/hermes_memory_eval`
`$TEST_DSN` = `postgres://hermes:$HERMES_PG_PASSWORD@127.0.0.1:5453/hermes_test`
`$INST_DSN` = `postgres://hermes:$HERMES_PG_PASSWORD@127.0.0.1:5452/hermes_memory_installed`

## Tests

```
.venv-replay/bin/python -m pytest -q tests/test_shadow_replay.py tests/test_load_config_dsn.py -m 'not integration' --tb=short
...........                                                              [100%]
11 passed, 1 deselected in 0.21s
```

```
.venv-replay/bin/python -m pytest -q tests/test_shadow_replay.py --migrate --dsn "$TEST_DSN" -o addopts= --tb=short
......                                                                   [100%]
6 passed in 0.58s
```

## Default-size eval — SIGKILL

First `eval_memory.py` at harness defaults (`facts=200 chatter=800 queries=150`) ingested then was killed.

```
eval_vector_start=2026-11-17T03:04:22-06:00
eval_vector_exit=137 eval_vector_end=2026-11-17T03:13:11-06:00
   0 .../eval-vector.json
1309 .../eval-vector.err
```

During that run, before kill:

```
n   | complete
------+----------
 2606 |     2606
```

stderr (complete file):

```
hybrid-age embed warmup failed:
Traceback (most recent call last):
  File "/home/rswan/Documents/Codex/hermes-memory/src/hermes_memory/provider.py", line 177, in initialize
    self._run(self.embedder.embed_text("warmup"), timeout=4.0)
...
TimeoutError
prefetch failed
...
TimeoutError
prefetch timeout query='Which project does MaraPellwex lead?'
```

No JSON was produced. Swap was exhausted (`Swap: 8.0Gi 8.0Gi 7.9Mi`).

## eval_memory.py vector (completing command)

```
eval_vector_start=2026-11-17T03:17:22-06:00
eval_vector_exit=0 eval_vector_end=2026-11-17T03:18:12-06:00
```

```json
{
  "retrieval_mode": "vector",
  "supersession_nyc_austin_nyc": {
    "query": "Where is my city now?",
    "sequence": "NYC->Austin->NYC",
    "latency_ms": 55.0,
    "has_nyc": true,
    "has_austin": true,
    "injected_chars": 3043
  },
  "corpus": {
    "facts": 40,
    "chatter": 80,
    "turns": 160,
    "queries": 40
  },
  "ingest": {
    "drain_seconds": 35.2,
    "turns_per_s": 4.54,
    "complete_rows": 326,
    "expected_rows": 326
  },
  "recall_prefetch": 1.0,
  "recall_vector_only": 1.0,
  "recall_by_kind": {
    "multihop": 1.0,
    "owner_lookup": 1.0,
    "lexical": 1.0
  },
  "noise_projects_per_answer_mean": 6.9,
  "injected_chars_p50": 3115,
  "entity_coverage": {
    "projects": 0.0,
    "owners": 1.0,
    "cities": 0.0
  },
  "leakage": {
    "other_owner_facts": 10,
    "answers_with_foreign_names": 0,
    "distinct_foreign_names": 0
  },
  "latency_ms": {
    "p50": 58.8,
    "p95": 81.3
  }
}
```

stderr: empty.

## eval_memory.py hybrid (completing command)

```
eval_hybrid_start=2026-11-17T03:18:34-06:00
eval_hybrid_exit=0 eval_hybrid_end=2026-11-17T03:19:36-06:00
```

```json
{
  "retrieval_mode": "hybrid",
  "supersession_nyc_austin_nyc": {
    "query": "Where is my city now?",
    "sequence": "NYC->Austin->NYC",
    "latency_ms": 172.7,
    "has_nyc": false,
    "has_austin": true,
    "injected_chars": 1657
  },
  "corpus": {
    "facts": 40,
    "chatter": 80,
    "turns": 160,
    "queries": 40
  },
  "ingest": {
    "drain_seconds": 41.9,
    "turns_per_s": 3.82,
    "complete_rows": 326,
    "expected_rows": 326
  },
  "recall_prefetch": 1.0,
  "recall_vector_only": 1.0,
  "recall_by_kind": {
    "multihop": 1.0,
    "owner_lookup": 1.0,
    "lexical": 1.0
  },
  "noise_projects_per_answer_mean": 5.97,
  "injected_chars_p50": 1903,
  "entity_coverage": {
    "projects": 0.0,
    "owners": 1.0,
    "cities": 0.0
  },
  "leakage": {
    "other_owner_facts": 10,
    "answers_with_foreign_names": 0,
    "distinct_foreign_names": 0
  },
  "latency_ms": {
    "p50": 185.1,
    "p95": 204.5
  }
}
```

stderr tail (shutdown race; eval still exited 0):

```
extraction step failed
Traceback (most recent call last):
  File "/home/rswan/Documents/Codex/hermes-memory/src/hermes_memory/extraction_worker.py", line 152, in run_extraction_once
    finished += await step()
  ...
  File ".../asyncpg/pool.py", line 1010, in _acquire
    raise exceptions.InterfaceError('pool is closing')
asyncpg.exceptions._base.InterfaceError: pool is closing
```

## shadow_replay.py against eval DB (`:5453`)

```
shadow_eval_start=2026-11-17T03:19:42-06:00
shadow_eval_exit=0 shadow_eval_end=2026-11-17T03:20:28-06:00
```

```
User turn counts per agent_identity:
  eval: 163
  eval-other: 20
Read 183 user turns (SELECT only)
{
  "turns_read": 183,
  "turns_compared": 183,
  "vector_errors": 0,
  "hybrid_errors": 0,
  "vector_error_rate": 0.0,
  "hybrid_error_rate": 0.0,
  "mean_jaccard_spans": 0.10695270537288636,
  "median_jaccard_spans": 0.043478260869565216,
  "pct_hybrid_adds_span": 100.0,
  "p50_vector_latency_ms": 57.41349887102842,
  "p95_vector_latency_ms": 68.7363960314542,
  "p50_hybrid_latency_ms": 192.98459286801517,
  "p95_hybrid_latency_ms": 218.2854781858623,
  "p95_facts_tokens": 635.0,
  "dry_run": false
}
```

Then: `docker rm -f hermes-eval-pg` → container removed.

## shadow_replay.py against installed DB (`:5452`, `--limit 500`)

```
shadow_installed_start=2026-11-17T03:20:34-06:00
shadow_installed_exit=0 shadow_installed_end=2026-11-17T03:21:17-06:00
```

```
User turn counts per agent_identity:
  default: 56
  librarian: 5
  agent:linkibinki:local:dm:local-292f132e9b6fb71470dd3401b9578c65efb5f13d5c453b4ffa243a50bf49953c: 2
  agent:linkibinki:local:dm:local-b605bfac516b843635d08e4d0bcb4fdf90e00322ac55095871d19d085b45f453: 1
Read 64 user turns (SELECT only)
{
  "turns_read": 64,
  "turns_compared": 64,
  "vector_errors": 0,
  "hybrid_errors": 0,
  "vector_error_rate": 0.0,
  "hybrid_error_rate": 0.0,
  "mean_jaccard_spans": 0.0,
  "median_jaccard_spans": 0.0,
  "pct_hybrid_adds_span": 0.0,
  "p50_vector_latency_ms": 116.54025106690824,
  "p95_vector_latency_ms": 1513.3516089990735,
  "p50_hybrid_latency_ms": 116.05712515302002,
  "p95_hybrid_latency_ms": 1265.2796609327197,
  "p95_facts_tokens": 0.0,
  "dry_run": false
}
```

Post-hoc SELECT on installed (no query text):

```
shadow_rows | vector_nonempty | hybrid_nonempty | avg_vector_ms | avg_hybrid_ms | avg_hybrid_tokens | avg_vector_tokens
          64 |              64 |               0 |   343.2359375 |   317.4671875 |                 0 |       1082.328125

SELECT status, count(*) FROM claims GROUP BY 1;
 status | count
--------+-------
(0 rows)
```

Hybrid ids/tokens are empty because `claims` has 0 rows. Vector ids were non-empty on all 64 rows. Jaccard/token zeros are empty-store, not swallowed exceptions.

## release_gate.py

```
Static gate: disposable source, HOME and Hermes home; child output withheld for privacy.
RUN disposable Git init
PASS disposable Git init: exit 0
RUN disposable Git snapshot
PASS disposable Git snapshot: exit 0
RUN disposable Git commit
PASS disposable Git commit: exit 0
RUN pytest (non-integration; no migrations)
PASS pytest (non-integration; no migrations): exit 0
RUN ruff (CI release scope)
PASS ruff (CI release scope): exit 0
RUN mypy (declared scope)
PASS mypy (declared scope): exit 0
RUN wheel (offline)
PASS wheel (offline): exit 0
PASS wheel entry-point metadata
RUN wheel provider registration
PASS wheel provider registration: exit 0
PASS static release checks; no live lifecycle or level-6 evidence.
release_gate_exit=0
default_retrieval_mode= vector
```

## §9 gate table

Thresholds quoted from `docs/superpowers/specs/2026-11-14-graph-layer-design.md` §9.
Spec: “All measured by `scripts/eval_memory.py` on its planted corpus, automatically (no human or model judge).”
The completing runs used `--facts 40 --chatter 80 --queries 40`, not the spec-sized 200 / 800 / 150 corpus (that default run was SIGKILL 137).

| Gate | Target (quoted) | This run | Verdict |
|---|---|---|---|
| Extraction recall (Phase 2) | ≥ 0.90. “Share of planted facts present as grounded, current claims with the correct subject and object entities. A store metric, not a retrieval metric.” | `eval_memory.py` does not emit this metric. | Unmeasurable |
| Single-fact recall | ≥ 0.95. “Share of single-fact questions whose gold names all appear in the injected text.” | On 40-query corpus: `recall_by_kind.lexical`/`owner_lookup` = 1.0 (vector and hybrid). Not the spec 150-question corpus. | Unmeasurable at spec corpus; 1.0 on the 40-query stand-in |
| Multi-hop recall | ≥ 0.80 (today 0.25). “The same, over the two-hop questions.” | `recall_by_kind.multihop` = 1.0 (vector and hybrid) on 40 queries. | Unmeasurable at spec corpus; 1.0 on the 40-query stand-in |
| Hybrid lift | ≥ +0.15 absolute. “Hybrid minus vector-only multi-hop recall, same questions, same corpus.” | `1.0 - 1.0 = 0.0` on the 40-query stand-in. Spec-sized run did not finish. | Unmeasurable at spec corpus; measured lift 0.0 on stand-in |
| Unrelated context | ≤ 2. “Mean count of planted project names in the injected text that are not on the gold path (the harness `noise` metric).” | vector `noise_projects_per_answer_mean` 6.9; hybrid 5.97. | Fail (both modes, this corpus) |
| Cross-owner leakage | 0. “Two owners with identical entity names and session ids; checked on every surface: turn ANN, claim ANN, linking, traversal, neighbours, facts block and evidence rendering, `turn_entity`, entity merges, job payloads and their context turns, `retrieval_shadow` rows.” | Harness `leakage.answers_with_foreign_names` = 0 (vector and hybrid). Other surfaces in the quoted matrix were not individually checked. | Unmeasurable for the full matrix; harness prefetch leakage 0 |
| Timelines under reordering | “current value matches the event-time timeline”. “Out-of-order backfill, repair of newest and middle values, span deletion, re-assertion of old values, contradictory polarity.” | Only NYC→Austin→NYC prefetch probe: vector `has_nyc=true has_austin=true`; hybrid `has_nyc=false has_austin=true`. No backfill/repair/delete suite. | Unmeasurable |
| Migration | “no unlisted state change”. §7.2 step 6. | Not run. | Unmeasurable |
| Extraction restart / replay / concurrency | “no duplicate evidence, no lost jobs”. “Kill workers mid-publish; replay; parallel workers on one session; concurrent creation of one entity.” | Not run. | Unmeasurable |
| Prefetch latency at 10k turns | p50 ≤ 150 ms, p95 ≤ 300 ms; per-arm budgets in §6. “Served path in `hybrid` mode.” | Not 10k turns. 40-query hybrid p50 185.1 / p95 204.5; vector p50 58.8 / p95 81.3. Installed shadow p95 vector 1513.35 / hybrid 1265.28 (n=64, not 10k). | Unmeasurable |
| Filtered recall with many owners | “no drop”. “Eval owner < 5% of rows versus a single-owner database.” | Not run. | Unmeasurable |
| Observability | “present”. “Health and verify report extraction jobs pending / leased / retryable / failed, blocked sessions, claims by status (including orphaned), and stale-status drift.” | Not run. | Unmeasurable |
| Secret redaction (Phase 6) | “100% of the secret corpus redacted on every surface; ≤ 2% of the look-alike corpus redacted”. | Phase 6 not implemented. | Unmeasurable |
