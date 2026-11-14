# Graph Layer Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute hybrid retrieval beside the vector path and record it, without changing the text prefetch returns.

**Architecture:** Fusion and a two-hop claim walk are pure. A shadow task runs after prefetch has already produced the vector block. It writes one `retrieval_shadow` row and never edits the returned string. `retrieval_mode` stays `vector` by default.

**Tech Stack:** Python 3.11+, asyncpg, Postgres 17 + pgvector, pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-graph-layer-design.md` §6 and the retrieval rows of §9. Shadow only. The served prompt stays vector memory with neighbour and claim overlays.

## Global Constraints

- `owner` means `conversations.agent_identity`.
- Prefetch's return value does not change in `vector` or `shadow` mode.
- Hybrid computation is not on the request path: it runs after the vector string is produced, and a failure there is logged.
- RRF k = 60. MMR λ = 0.7.
- Traversal: at most 2 hops, at most 8 edges per node, at most 20 claims per hop.
- Shadow rows store ids and latencies, not query text. The query key is 16 bytes.
- SQL values are bind parameters.
- Embeddings stay 768-d.
- Integration tests use `.superpowers/sdd/pytest-hermes-test.sh` and `hermes_test` only.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: Reciprocal rank fusion and MMR

**Files:**
- Create: `src/hermes_memory/fusion.py`
- Test: `tests/test_fusion.py`

**Interfaces:**
- `def rrf(rankings: Sequence[Sequence[str]], *, k: int = 60) -> list[str]`
- `def mmr(ids: Sequence[str], vectors: dict[str, Sequence[float]], *, lam: float = 0.7, limit: int) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
from hermes_memory.fusion import mmr, rrf


def test_rrf_promotes_an_id_that_appears_in_two_lists():
    fused = rrf([["a", "b", "c"], ["b", "d"]], k=60)
    assert fused[0] == "b"


def test_mmr_keeps_the_first_and_skips_a_near_duplicate():
    vectors = {"a": [1.0, 0.0], "a2": [0.99, 0.01], "c": [0.0, 1.0]}
    picked = mmr(["a", "a2", "c"], vectors, lam=0.7, limit=2)
    assert picked[0] == "a"
    assert "c" in picked
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/home/rswan/miniconda3/bin/pytest tests/test_fusion.py -q`
Expected: FAIL (`fusion` missing)

- [ ] **Step 3: Implement RRF and cosine MMR**

RRF score of an id is the sum of `1 / (k + rank)` over lists it appears in, rank starting at 1. Higher score first. Ties break by id. MMR picks the highest remaining score of `lam * relevance + (1 - lam) * (1 - max cosine to an already picked vector)`. Relevance is `1 / (1 + index in ids)`.

- [ ] **Step 4: Run the tests to verify they pass**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(retrieval): fuse rankings with RRF and diversify with MMR

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Shadow log table

**Files:**
- Create: `sql/migrations/V20__retrieval_shadow.sql`
- Modify: `src/hermes_memory/schema_guard.py` (append `"V20"`)
- Test: `tests/test_schema_guard.py` (assert `"V20"`)

**Interfaces:**
- Table `retrieval_shadow(id, owner, query_key bytea, vector_ids jsonb, hybrid_ids jsonb, latency_ms jsonb, created_at)`.
- `query_key` is 16 bytes. No query text column.

- [ ] **Step 1: Write the failing assertion** `assert "V20" in CONSUMER_HEAD_VERSIONS`
- [ ] **Step 2: Run** `/home/rswan/miniconda3/bin/pytest tests/test_schema_guard.py -q` and confirm that assertion fails.
- [ ] **Step 3: Add the idempotent migration and the head pin.**
- [ ] **Step 4: Re-run the schema-guard test.** Expected: PASS
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(retrieval): add the retrieval_shadow log

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Prefetch still returns the vector block

**Files:**
- Create: `src/hermes_memory/shadow.py`
- Modify: `src/hermes_memory/provider.py` (`_aprefetch` only schedules; it returns the same string)
- Test: `tests/test_shadow_prefetch.py`

**Interfaces:**
- `def query_key(secret: bytes, text: str) -> bytes` returns 16 bytes of HMAC-SHA256 over the stripped query.
- `async def record_shadow(conn, owner: str, key: bytes, vector_ids: list, hybrid_ids: list, latency_ms: dict) -> None`
- Prefetch in the default mode returns its current string and does not await `record_shadow`.

- [ ] **Step 1: Write a failing test** that `query_key` is 16 bytes and stable for the same secret and text, and that two secrets differ.
- [ ] **Step 2: Run it and confirm failure.**
- [ ] **Step 3: Implement `query_key` and `record_shadow`.** Insert with bind parameters. Reject a key whose length is not 16.
- [ ] **Step 4: Add a provider test** that a fake prefetch path returns the vector string unchanged when a shadow hook is attached and raises. The hook must not be awaited before the return.
- [ ] **Step 5: Run** `/home/rswan/miniconda3/bin/pytest tests/test_shadow_prefetch.py -q`
- [ ] **Step 6: Commit**

```bash
git commit -m "feat(retrieval): record shadow rows without changing prefetch text

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Bounded claim walk

Spec §6 step 3, pure so it can be tested without a database.

**Files:**
- Create: `src/hermes_memory/claim_walk.py`
- Test: `tests/test_claim_walk.py`

**Interfaces:**
- `def walk(start: Sequence[str], edges: dict[str, list[tuple[str, str]]], *, hops: int = 2, per_node: int = 8, per_hop: int = 20) -> list[str]`
- `edges[entity]` is a list of `(claim_id, next_entity)` already ordered by relevance.
- Returns claim ids. Stops at `hops`. Takes at most `per_node` edges from each entity and at most `per_hop` claims per hop. Does not revisit an entity.

- [ ] **Step 1: Write a failing test** with a chain a→b→c and a cycle back to a. Two hops from `a` include the claims on a and b, and do not loop.
- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement `walk`.**
- [ ] **Step 4: Confirm pass** with `/home/rswan/miniconda3/bin/pytest tests/test_claim_walk.py -q`
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(retrieval): bound the claim walk at two hops

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
