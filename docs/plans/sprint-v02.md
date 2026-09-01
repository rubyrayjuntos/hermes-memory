# Sprint: Persistent Memory & Prompt Context Injection (v0.2 kickoff)

**Spec:** [`docs/specs/persistent-memory-spec.md`](persistent-memory-spec.md) (canonical, amended)
**Process:** same gauntlet — one implementer, spec review → quality review → delta re-review
**Runs in parallel with:** Phase-2 production install (`phase2-install.md`)

## Amendments applied to Ray's spec (agreed 2026-08-25)

1. All reference code adapts to the **actual v0.1 schema** during implementation
   (BIGINT ids, `content`, bridge `(chunk_id TEXT, source TEXT, vertex_id BIGINT)`,
   graph links via bridge not edge properties).
2. DLQ threshold stays at **attempts ≥ 5** (watchdog consistency; spec said 3).
3. CI gates (**Injection-Hit ≥0.85**, MRR regression ≤0.05) are **advisory until
   golden set ≥ 50 items**.
4. Recency scoring requires a real time-decay implementation before weight
   optimization profiles are meaningful.
5. Deferred to v0.3+: RLS multi-tenancy + isolation suite, Prometheus metrics,
   PgBouncer, 180-day retention purge.

## Status 2026-09-01 — v0.2 production-ready plugin (code DONE, soak for tuning)
> C7/C8 code DONE per `board.md` (`df4e077`/`1a8a99f`); shown as `[ready]` here for Sprint 2 re-run on accumulated live corpus (699v/1936e). C9/C10 intentionally `blocked` until soak data available. Source of truth for queuing is GitHub Project #6 Sprint 2 (not this file).

## Board

### C7 — Benchmark harness `[code DONE — soak re-run in Sprint 2]`
Adapt Appendix D benchmarker to real schema. Deliverables:
- Golden-set builder: extract (query → expected memory) pairs from real session logs
- Metrics: Retrieval-Hit Rate, Injection-Hit Rate, Injection MRR, empty-block rate,
  p50/p95/p99 latency, avg tokens, context efficiency (per spec §4/§12)
- Runners: `--ablation`, `--optimize-weights`, `--optimize-budget`, `--throughput`
- tiktoken (cl100k_base) exact token counting for truncation
- Graph expansion statement_timeout 200ms guard
- Initial golden set: Ray's 10 items (Appendix A) + grown from real logs toward 50

**Accept:** harness runs against live compose stack; ablation produces the
Hybrid-vs-Vector-only table; all four metric families reported.

### C8 — Synthetic load generator `[code DONE — soak regen in Sprint 2]`
Adapt Appendix 12.1 to real schema + bridge-table population.
Sizes 1K / 10K / 100K rows; batched MERGE per §3.3; cleans up after itself
(`--teardown` flag removing synthetic rows by marker prefix).

**Accept:** generates 10K-row corpus in <5 min without savepoint poisoning;
latency profile runnable at each size; teardown leaves zero synthetic rows.

### C9 — Production install execution `[blocked on: Phase-2 pre-flight — unblock in Sprint 2 soak]`
Execute phase2-install.md sequence with verification gate V1–V5.
Benchmarks from C7 run as part of V-gate before crons re-enable.

### C10 — Tuning experiments `[blocked on: C7 + real data — tuning on live graph/vector build]`
Weight profiles (Baseline/Recency/Graph/Balanced) and budget scaling
(500→9999) on live accumulated corpus. Output: chosen ranking profile +
token budget recorded in config + README.

---

**Sprint rule:** C7 and C8 are code DONE (soak re-runs). C9/C10 fine-tuning tracks graph/vector build density — intentionally held open post-v0.2 to observe DB growth before weight/budget lock-in. GitHub issues #9/#12/#14/#21/#28 are backlog queued to Sprint 2 for this soak.
