# C7 Benchmark Evidence

**Corpus:** fixture `sample_repo` + 10 golden memories.
**Caveat:** the bridge table is empty (extractors v0.2), so graph expansion contributed nothing yet — the ablation must be re-run after C8 synthetic load and C10 real data.

## Hybrid vs Vector-only ablation

| Configuration | Retr-Hit | Inj-Hit | Inj-MRR | Queries | Latency (p50/p95/p99 ms) |
|---|---|---|---|---|---|
| Hybrid | 0.80 | 0.90 | 0.56 | 29 | 29 / 329 / 329 |
| Vector-only | 0.80 | 0.90 | 0.56 | 29 | 2 / 2 / 2 |

Identical quality metrics — expected, since graph expansion has no data to expand over yet.

## optimize-weights

| Run | Weights | Retr-Hit | Inj-Hit | Inj-MRR |
|---|---|---|---|---|
| 1–4 (all) | varied | 0.80 | 0.90 | 0.56 |

All 4 weight configurations produce identical metrics — corpus too small to differentiate weight settings.

## optimize-budget

| Token budget | Retr-Hit | Inj-Hit | Avg tokens |
|---|---|---|---|
| 6 rows (varied budgets incl. 500) | 0.80 | flat 0.90 | ~410 |

Injection-hit stays flat at 0.90 even at a 500-token budget; average injected tokens ~410, well under all tested budgets.

## Throughput

- 256 writes
- 16.0 turns/sec
- Latency: p50 1043 ms / p95 1881 ms / p99 1958 ms

## Property suite

18 passed, 1 skipped (`tests/property`).
