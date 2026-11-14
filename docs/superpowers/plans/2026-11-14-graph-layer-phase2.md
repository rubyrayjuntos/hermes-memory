# Graph Layer Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run rules and model extraction as durable jobs off the prompt path, fill `fact_vec`, and let a newer model tier supersede only the rules evidence it agrees with.

**Architecture:** After a new turn row is stored, the drain enqueues two id-only jobs (`extract_rules`, then `extract_model`). A worker claims them with the Phase 1 fence, publishes evidence in one transaction, and embeds the resulting current claims. Phase 1 inline reception stays; the rules job is idempotent with it. The model tier never runs during prefetch.

**Tech Stack:** Python 3.11+, asyncpg, Postgres 17 + pgvector, pytest. Ollama is behind `ModelExtractor`; tests use a fake.

**Spec:** `docs/superpowers/specs/2026-11-14-graph-layer-design.md` (v0.5.1) §4.1 grounding, §5.1–§5.2, §9 extraction rows.

## Global Constraints

- `owner` means `conversations.agent_identity`, everywhere (spec §2.2).
- Extraction never runs on the prompt path. The prompt path makes no model calls other than one query embedding.
- Job payloads carry ids only: `span_id`, `extractor`, `version`. No turn text.
- `op_id` is `{span_id}:{extractor}:{version}`.
- Leases: `extract_rules` 60 s, `extract_model` 120 s. Heartbeat every quarter lease, only while the fence is current.
- Backoff 5 s, 30 s, 120 s, 600 s; failed after 5 attempts.
- Publish locks the job row and checks the fence before writing evidence. A stale fence writes nothing.
- Live jobs have priority 0. Backfill has priority 10 and is not claimed while a priority-0 job of the same kind is `pending`, `leased`, or `retryable`.
- Rules identity stays `extractor='rules'`, `extractor_version='1'`. Model identity is `extractor='model'`, `extractor_version='1'`.
- A model claim supersedes a rules evidence row of the same span only when both resolve to the same timeline key and the same object value. Other rules evidence stays.
- Model output names must appear, normalized, in the turn or its four previous context turns. Otherwise the claim is dropped.
- Embeddings are nomic-embed-text, 768-d. `fact_vec` is written only by this phase's embed step, with `embed_model` stamped.
- Distinct timeline locks are taken in sorted `(owner, subject_id, verb)` order before evidence writes.
- SQL values are bind parameters. Never interpolate user text into SQL.
- Integration tests run only against `hermes_test` via `.superpowers/sdd/pytest-hermes-test.sh`. Unit tests: `/home/rswan/miniconda3/bin/pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A stale fence publishes nothing.
2. The same span processed twice does not duplicate evidence.
3. A model claim does not delete a rules claim it did not produce.
4. A model name that is not in the turn or the four previous turns is dropped.
5. Backfill waits while a live job of that kind is still open.
6. Prefetch does not call the extractor.

---

### Task 1: Enqueue extraction jobs for a new turn

Spec §5.2 opening paragraph and §5.1 payloads.

**Files:**
- Create: `src/hermes_memory/extraction_jobs.py`
- Modify: `src/hermes_memory/provider.py` (after a new turn is inserted, before the flower link)
- Test: `tests/test_extraction_jobs.py`

**Interfaces:**
- Produces: `RULES_VERSION = "1"`, `MODEL_VERSION = "1"`, `def job_specs(span_id: int) -> tuple[tuple[str, str, dict], ...]`
- Each spec is `(kind, op_id, payload)` with payload `{"span_id", "extractor", "version"}` only.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extraction_jobs.py
from hermes_memory.extraction_jobs import job_specs


def test_a_new_span_enqueues_rules_then_model_with_ids_only():
    specs = job_specs(42)
    assert specs == (
        ("extract_rules", "42:rules:1", {"span_id": 42, "extractor": "rules", "version": "1"}),
        ("extract_model", "42:model:1", {"span_id": 42, "extractor": "model", "version": "1"}),
    )
    for _kind, _op, payload in specs:
        assert set(payload) == {"span_id", "extractor", "version"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/home/rswan/miniconda3/bin/pytest tests/test_extraction_jobs.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation and call it from the drain**

```python
# src/hermes_memory/extraction_jobs.py
"""Id-only extraction jobs for one stored turn (spec §5.2)."""
from __future__ import annotations

RULES_VERSION = "1"
MODEL_VERSION = "1"


def job_specs(span_id: int) -> tuple[tuple[str, str, dict], ...]:
    span = int(span_id)
    return (
        ("extract_rules", f"{span}:rules:{RULES_VERSION}",
         {"span_id": span, "extractor": "rules", "version": RULES_VERSION}),
        ("extract_model", f"{span}:model:{MODEL_VERSION}",
         {"span_id": span, "extractor": "model", "version": MODEL_VERSION}),
    )
```

In `provider.py`, after reception and only when `inserted_new` is true, call `job_specs(conv_id)` and `store.job_add` for each spec with `session_id`, `event_time=item.get("captured_at")`, and `priority=0`. Swallow and log exceptions. Do not enqueue on the prefetch path. Skip when the store has no `job_add`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `/home/rswan/miniconda3/bin/pytest tests/test_extraction_jobs.py tests/test_reception_stage.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(extract): enqueue rules and model jobs for a new turn

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Fill fact vectors for current claims

Spec §5.2 step 3 (embed) and step 6 (stamp `embed_model`).

**Files:**
- Modify: `src/hermes_memory/store_claims.py`
- Test: `tests/integration/test_fact_embed.py`

**Interfaces:**
- Produces: `async def claims_missing_vectors(conn, owner: str, claim_ids: Sequence[int]) -> list[tuple[int, str]]`
- Produces: `async def write_fact_vectors(conn, rows: Sequence[tuple[int, list[float]]], embed_model: str) -> int`
- A row is eligible only when `status='grounded'`, `valid_to IS NULL`, `fact_text IS NOT NULL`, and `fact_vec IS NULL`.

- [ ] **Step 1: Write the failing test**

Insert one grounded current claim with `fact_text` and a null vector, plus one superseded claim with text. `claims_missing_vectors` returns only the current id. `write_fact_vectors` stores a 768-d list and `embed_model='nomic-embed-text'`. The superseded row stays null.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.superpowers/sdd/pytest-hermes-test.sh tests/integration/test_fact_embed.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL (`claims_missing_vectors` missing)

- [ ] **Step 3: Implement the two functions**

Select and update with bind parameters. Refuse a vector whose length is not 768 (`ValueError`). Do not write `valid`.

- [ ] **Step 4: Run the test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(claims): embed current fact text into fact_vec

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Rules publish is fence-checked and idempotent

Spec §5.1 publish and §5.2 steps 1–3 for the rules tier.

**Files:**
- Create: `src/hermes_memory/extract_publish.py`
- Test: `tests/integration/test_extract_publish.py`

**Interfaces:**
- `async def publish_rules(conn, store_embed, job_id: int, fence: int) -> list[int]`
- Reads the turn by `payload.span_id` and owner. Context is not required for rules.
- Calls `job_fence_check` first. A false check returns `[]` and writes no evidence.
- Uses `extract_assertions` and `record`'s evidence path with `extractor='rules'`, `version='1'`.
- Then fills `fact_vec` for the claim ids just touched, using the injected async `store_embed(texts) -> list[list[float] | None]`.
- Marks the job done only when the fence still matches.

- [ ] **Step 1: Write failing tests**

1. A user span `"Acme uses Postgres for the ledger service."` published once yields one current grounded claim and a 768-d `fact_vec`.
2. Publishing the same job twice does not add a second evidence row.
3. After `job_done`, a second publish with the old fence writes nothing.

Use the claims helpers `span` and `current`. Build the job with `job_add` then `job_claim`.

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL (`publish_rules` missing)

- [ ] **Step 3: Implement `publish_rules`**

Lock timelines through the existing `record_rule_claims` path (it already sorts locks). Do the embed update in the same transaction after the fence check. If the fence check fails, roll back.

- [ ] **Step 4: Run the test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(extract): fence-checked rules publish fills fact vectors

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Model output grounding

Spec §4.1 last paragraph and §5.2 step 2.

**Files:**
- Create: `src/hermes_memory/model_extract.py`
- Test: `tests/test_model_extract.py`

**Interfaces:**
- `def grounded_claims(raw: dict, turn: str, context: Sequence[str], verbs: set[str]) -> list[dict]`
- Keeps a claim only when `verb` is in `verbs`, subject and object normalize to a non-empty surface, and that normalized surface occurs in the normalized turn or context.
- Drops pronouns and generics via `is_abstained`, except first person, which is kept.
- Output dicts match `record_rule_claims` items: `subject`, `verb`, `object`, `polarity`.

- [ ] **Step 1: Write the failing tests**

```python
from hermes_memory.model_extract import grounded_claims

VERBS = {"uses", "lives_in"}


def test_name_must_appear_in_the_turn_or_context():
    raw = {"claims": [
        {"subject": "Acme", "verb": "uses", "object": "Postgres", "polarity": "positive"},
        {"subject": "Acme", "verb": "uses", "object": "Oracle", "polarity": "positive"},
    ]}
    kept = grounded_claims(raw, "Acme uses Postgres.", ["Earlier we mentioned Postgres."], VERBS)
    assert [c["object"] for c in kept] == ["Postgres"]


def test_unknown_verb_and_pronoun_subject_are_dropped():
    raw = {"claims": [
        {"subject": "Acme", "verb": "likes", "object": "Postgres", "polarity": "positive"},
        {"subject": "it", "verb": "uses", "object": "Postgres", "polarity": "positive"},
    ]}
    assert grounded_claims(raw, "Acme uses Postgres. it uses Postgres.", [], VERBS) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL (`model_extract` missing)

- [ ] **Step 3: Implement `grounded_claims`**

Pure. No network. Invalid JSON shapes return `[]`.

- [ ] **Step 4: Run the test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(extract): drop model claims that are not grounded in the turn

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Model publish supersedes only matching rules evidence

Spec §5.2 steps 3–4.

**Files:**
- Modify: `src/hermes_memory/extract_publish.py`
- Test: `tests/integration/test_model_publish.py`

**Interfaces:**
- `async def publish_model(conn, claims: list[dict], job_id: int, fence: int) -> list[int]`
- Evidence uses `extractor='model'`, `version='1'`.
- For each kept model claim, a live rules evidence row of the same span, timeline key, and object value becomes `state='superseded_version'`.
- A rules claim with a different object stays live.
- Stale fence writes nothing.

- [ ] **Step 1: Write the failing tests**

1. Rules claim "Acme uses Postgres" plus a model claim with the same value: one current claim, rules evidence `superseded_version`, model evidence `live`.
2. A second rules claim "Acme uses Redis" stays live when the model only produced Postgres.
3. Stale fence does not change evidence states.

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL (`publish_model` missing)

- [ ] **Step 3: Implement `publish_model`**

Fence check first. Sorted timeline locks before evidence writes. Rebuild the touched keys. Do not embed inside this function (Task 2's helper may be called by the worker later; this task asserts evidence state).

- [ ] **Step 4: Run the test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(extract): model evidence supersedes only the rules claim it matches

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Re-extraction diff

Spec §5.2 step 5.

**Files:**
- Create: `src/hermes_memory/extraction_diff.py`
- Test: `tests/test_extraction_diff.py`

**Interfaces:**
- `@dataclass(frozen=True) class DiffReport(lost: tuple[str, ...], added: tuple[str, ...])`
- `def diff_claims(before: set[str], after: set[str]) -> DiffReport`
- Keys are `"{subject}|{verb}|{object}|{polarity}"`. `lost` is sorted before-not-after. `added` is sorted after-not-before.

- [ ] **Step 1: Write the failing test**

```python
from hermes_memory.extraction_diff import diff_claims


def test_diff_reports_claims_lost_and_added():
    report = diff_claims({"Acme|uses|Postgres|positive", "Acme|uses|Redis|positive"},
                         {"Acme|uses|Postgres|positive", "Acme|uses|SQLite|positive"})
    assert report.lost == ("Acme|uses|Redis|positive",)
    assert report.added == ("Acme|uses|SQLite|positive",)
```

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL

- [ ] **Step 3: Implement `diff_claims`**

Pure. Empty difference yields empty tuples.

- [ ] **Step 4: Run the test to verify it passes**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(extract): report claims lost and added by a re-extraction

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Backfill waits for live jobs

Spec §5.1 priority.

**Files:**
- Modify: `src/hermes_memory/store_jobs.py` (`job_claim`)
- Test: `tests/integration/test_durable_job.py` (append)

**Interfaces:**
- `job_claim(..., defer_backfill: bool = False)`
- When true, a row with `priority > 0` is not claimed while any row of the same kind with `priority = 0` is `pending`, `leased`, or `retryable`.

- [ ] **Step 1: Write the failing test**

Add a live `extract_rules` job at priority 0 and a backfill job at priority 10. `job_claim(..., defer_backfill=True)` returns only the live job. After `job_done` on it, a second claim returns the backfill job.

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL (both jobs claimed, or `defer_backfill` unexpected)

- [ ] **Step 3: Add the SQL predicate**

Bind the flag. Do not change capture claims that pass `defer_backfill=False`.

- [ ] **Step 4: Run durable-job tests**

Expected: PASS, including the existing tests.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(jobs): hold backfill while a live extraction job is open

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Worker loop stays off the prompt path

Spec §5.2 "Extraction is a backlog, never on the prompt path."

**Files:**
- Modify: `src/hermes_memory/provider.py`
- Test: `tests/test_extraction_worker.py`

**Interfaces:**
- `async def run_extraction_once(provider, store, *, embed, model) -> int` returns jobs finished.
- Claims at most one `extract_rules` job (lease 60) and one `extract_model` job (lease 120), `session_ordered=True`, `defer_backfill=True`.
- Rules publish uses `publish_rules`. Model publish loads the span and four previous turns, calls `model(turn, context)`, then `grounded_claims`, then `publish_model`.
- `prefetch` does not call `run_extraction_once`. Assert by a test that wraps `run_extraction_once` and runs prefetch.

- [ ] **Step 1: Write the failing test**

A fake store records `job_claim` kinds. `run_extraction_once` claims `extract_rules` and `extract_model`. A prefetch call on a provider whose extract hook increments a counter leaves the counter at 0.

- [ ] **Step 2: Run the test to verify it fails**

Expected: FAIL

- [ ] **Step 3: Implement the one-shot worker and start it from `_ainit` as its own task, separate from prefetch**

Exceptions in the worker are logged. They do not fail prefetch.

- [ ] **Step 4: Run the unit test**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(extract): run extraction on a worker, not on prefetch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
