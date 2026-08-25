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

## Board

### C7 — Benchmark harness `[ready]`
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

### C8 — Synthetic load generator `[ready]`
Adapt Appendix 12.1 to real schema + bridge-table population.
Sizes 1K / 10K / 100K rows; batched MERGE per §3.3; cleans up after itself
(`--teardown` flag removing synthetic rows by marker prefix).

**Accept:** generates 10K-row corpus in <5 min without savepoint poisoning;
latency profile runnable at each size; teardown leaves zero synthetic rows.

### C9 — Production install execution `[blocked on: Phase-2 pre-flight]`
Execute phase2-install.md sequence with verification gate V1–V5.
Benchmarks from C7 run as part of V-gate before crons re-enable.

### C10 — Tuning experiments `[blocked on: C7 + real data post-install]`
Weight profiles (Baseline/Recency/Graph/Balanced) and budget scaling
(500→9999) on live accumulated corpus. Output: chosen ranking profile +
token budget recorded in config + README.

---

**Sprint rule:** C7 and C8 are code-complete-independent but both need the
compose stack — run sequentially. Phase-2 pre-flight (P1–P3) can proceed now.
