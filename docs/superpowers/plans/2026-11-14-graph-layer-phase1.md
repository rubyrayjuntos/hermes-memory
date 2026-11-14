# Graph Layer Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `claims` an owner-scoped, evidence-derived store with entities and event-time timelines, move reception onto it, migrate existing claims without changing their meaning, and move write capture onto one durable job table.

**Architecture:** Two pure modules carry the semantics: `entities.py` (which surfaces may name an entity) and `timeline.py` (one key's evidence events → planned claim rows). A store mixin, `store_claims.py`, loads evidence, calls the pure rebuild and applies the plan in the same transaction, under a per-key advisory lock. Reception keeps its inline place in the drain but writes evidence through that mixin. `durable_job` replaces `write_outbox` behind the existing `outbox_*` methods, so the provider is untouched.

**Tech Stack:** Python 3.11+, asyncpg, Postgres 17 + pgvector 0.8.6 + Apache AGE 1.6.0, pytest, pytest-asyncio, hypothesis.

**Spec:** `docs/superpowers/specs/2026-11-14-graph-layer-design.md` (v0.5.1, approved). Every task cites the section it implements. Read §4.1, §4.2, §5.1, §5.3 and §7.2 before starting.

## Global Constraints

- `owner` means `conversations.agent_identity`, everywhere (spec §2.2).
- Migrations under `sql/migrations/` are idempotent: `IF NOT EXISTS`, `CREATE OR REPLACE`, `DO $$ … pg_constraint …` guards for constraints (AGENTS.md).
- Dynamic SQL identifiers via `psycopg.sql.Identifier` or fixed strings; never interpolate user text into SQL (AGENTS.md).
- Embeddings are nomic-embed-text, 768-d; `fact_vec` stays NULL in Phase 1 (spec §5.2 step 3).
- Timelines rebuild in the transaction that changed their evidence, holding `pg_advisory_xact_lock(hashtextextended('timeline|<owner>|<subject_id>|<verb>', 0))`; take locks for several keys in sorted order.
- Entity, alias and `turn_entity` inserts use `INSERT … ON CONFLICT DO NOTHING RETURNING` then `SELECT` (spec §4.1).
- The `valid` column is written only by the `claims_valid_mirror` trigger after V18 (spec §4.2 "Legacy columns").
- Phase 1 extractor identity: `extractor = 'rules'`, `extractor_version = '1'`; migrated rows use version `'legacy'`.
- Capture lease 600 s; backoff 5 s, 30 s, 120 s, 600 s; 5 attempts (spec §5.1).
- Integration tests run only against `hermes_test`: `pytest tests/integration -q -m integration --override-ini="addopts=" --migrate`. Unit tests: `pytest -q`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Two writers rebuilding one key at once** (the provider drain and a catch-up pass, or two processes): exactly one current row results. The advisory lock in `_rebuild` serializes them; Task 5 has a concurrent-writer test.
2. **The same span processed twice** (`catch_up_reception` after a crash mid-stage): no duplicate evidence, same claim ids. Task 5 tests a re-run.
3. **A legacy retraction whose superseding row belongs to another owner** (V15 de-duplicated across owners when the key was global): the retract lands on the claim's own span, never across owners. Task 9 tests it.
4. **Surfaces that normalize to nothing** (punctuation only, emoji, non-Latin text the normalizer strips): they abstain instead of creating an entity named `''`. Task 1 tests it.
5. **A fact repeated hundreds of times**: one key with 500 events rebuilds in under 200 ms. Task 2 has the timing test.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/hermes_memory/entities.py` (new) | Pure surface rules: normalization, abstention, first person, object kind |
| `src/hermes_memory/timeline.py` (new) | Pure timeline rebuild: events → planned rows |
| `sql/migrations/V18__claims_evidence.sql` (new) | Entities, aliases, verbs, evidence, new claim columns, mirror trigger, data_migrations |
| `sql/migrations/V19__durable_job.sql` (new) | `durable_job`, copy of pending `write_outbox` rows |
| `src/hermes_memory/store_claims.py` (new) | Conn-level claim functions (resolve, evidence, rebuild, repair, aliases, merges, delete, drift) and `StoreClaimsMixin` |
| `src/hermes_memory/store_jobs.py` (new) | `StoreJobsMixin`: generic durable jobs plus the `outbox_*` capture facade |
| `src/hermes_memory/claims_migration.py` (new) | One-shot legacy data migration and its §7.2 check |
| `src/hermes_memory/store.py` | Mix in the two new mixins; remove the old claim and alias writers and the `write_outbox` methods; new `span_overlays` |
| `src/hermes_memory/reception.py` | New `ReceptionStore` protocol |
| `src/hermes_memory/provider.py` | `_reception_stage` writes evidence; `_ainit` runs the data migration |
| `src/hermes_memory/schema_guard.py` | Head versions add V18, V19 |
| `src/hermes_memory/verify.py`, `graph_runtime.py` | Cleanup through `delete_spans`; claims and jobs counts; drift repair |
| `scripts/eval_memory.py` | Durability check reads `durable_job` |
| `tests/conftest.py` | Truncate the new tables |
| Tests | `tests/test_entities.py`, `tests/test_timeline.py`, `tests/property/test_timeline_properties.py`, `tests/integration/claims_helpers.py`, `tests/integration/test_entities_db.py`, `tests/integration/test_claims_timeline.py`, `tests/integration/test_claims_repair.py`, `tests/integration/test_claims_aliases.py`, `tests/integration/test_delete_spans.py`, `tests/integration/test_claims_migration.py`, `tests/integration/test_durable_job.py`, plus updates to `tests/test_reception_stage.py`, `tests/integration/test_outbox.py`, `tests/test_schema_guard.py`, `tests/test_graph_api.py` |

---

### Task 1: Entity surface rules

Spec §4.1 "Abstention", §4.2 (literal vs entity objects).

**Files:**
- Create: `src/hermes_memory/entities.py`
- Test: `tests/test_entities.py`

**Interfaces:**
- Consumes: `hermes_memory.reception.normalize_surface(text: str) -> str`
- Produces:
  - `normalize_name(surface: str) -> str`
  - `is_first_person(surface: str) -> bool`
  - `is_abstained(surface: str) -> bool`
  - `classify_object(surface: str) -> Literal["entity", "literal", "abstain"]`
  - constants `ABSTAIN_SURFACES`, `SELF_NAME = "(self)"`, `SELF_NORM = ""`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_entities.py
"""Surface rules for entities (graph layer spec §4.1)."""
import pytest

from hermes_memory.entities import (
    classify_object,
    is_abstained,
    is_first_person,
    normalize_name,
)


@pytest.mark.parametrize("surface", [
    "he", "She", "they", "it", "We", "you", "them", "this", "That", "those",
    "someone", "something", "the project", "The App", "the repo", "the team",
    "the company", "the thing", "the user", "of the", "", "   ", "!!!", "🙂",
])
def test_pronouns_demonstratives_generics_and_empty_abstain(surface):
    assert is_abstained(surface)


@pytest.mark.parametrize("surface", ["Raul", "Acme Corp", "Tokyo Eye", "Postgres", "project Hermes"])
def test_names_do_not_abstain(surface):
    assert not is_abstained(surface)


@pytest.mark.parametrize("surface", ["I", "me", "My", "myself"])
def test_first_person_is_not_abstained_and_is_detected(surface):
    assert is_first_person(surface)
    assert not is_abstained(surface)


def test_normalize_name_matches_reception_normalizer():
    assert normalize_name("  Tokyo-Eye ") == "tokyo eye"


@pytest.mark.parametrize("surface,kind", [
    ("Acme", "entity"),
    ("Acme Corp", "entity"),
    ("Austin", "entity"),
    ("me", "entity"),
    ("a vector database for memory", "literal"),
    ("42", "literal"),
    ("2026-01-01", "literal"),
    ("it", "abstain"),
    ("the project", "abstain"),
])
def test_classify_object(surface, kind):
    assert classify_object(surface) == kind
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_entities.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_memory.entities'`

- [ ] **Step 3: Write the implementation**

```python
# src/hermes_memory/entities.py
"""Which surfaces may name an entity (graph layer spec §4.1).

Pure. Pronouns, demonstratives, indefinites and generic nouns never create
or match an entity; first-person singular resolves to the owner's self
entity. Anything the normalizer reduces to nothing abstains.
"""
from __future__ import annotations

import re
from typing import Literal

from .reception import normalize_surface

SELF_NAME = "(self)"
SELF_NORM = ""

FIRST_PERSON = frozenset({"i", "me", "my", "mine", "myself"})
PRONOUNS = frozenset({
    "he", "him", "his", "himself", "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves", "it", "its", "itself",
    "we", "us", "our", "ours", "ourselves", "you", "your", "yours",
    "yourself", "yourselves",
})
DEMONSTRATIVES = frozenset({"this", "that", "these", "those"})
INDEFINITES = frozenset({
    "someone", "somebody", "something", "anyone", "anybody", "anything",
    "everyone", "everybody", "everything", "nobody", "nothing",
})
ABSTAIN_SURFACES = PRONOUNS | DEMONSTRATIVES | INDEFINITES
GENERIC_HEADS = frozenset({
    "project", "app", "application", "repo", "repository", "team", "company",
    "thing", "user", "system", "tool", "service", "code", "stuff", "one",
})
DETERMINERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "my", "our", "your",
    "his", "her", "their", "its",
})
STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "do", "does", "did", "where", "what", "who", "which",
    "when", "why", "how", "with", "at", "by", "from",
})

_NAME_RE = re.compile(r"[A-Z][\w\-]*(?:\s+[A-Z][\w\-]*){0,3}")


def normalize_name(surface: str) -> str:
    return normalize_surface(surface)


def is_first_person(surface: str) -> bool:
    return normalize_name(surface) in FIRST_PERSON


def is_abstained(surface: str) -> bool:
    norm = normalize_name(surface)
    if not norm:
        return True
    if norm in FIRST_PERSON:
        return False
    if norm in ABSTAIN_SURFACES:
        return True
    words = norm.split()
    if all(w in STOPWORDS or w in ABSTAIN_SURFACES for w in words):
        return True
    return len(words) == 2 and words[0] in DETERMINERS and words[1] in GENERIC_HEADS


def classify_object(surface: str) -> Literal["entity", "literal", "abstain"]:
    raw = (surface or "").strip()
    if is_abstained(raw):
        return "abstain"
    if is_first_person(raw) or _NAME_RE.fullmatch(raw):
        return "entity"
    return "literal"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_entities.py -q`
Expected: PASS (all parametrized cases)

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/entities.py tests/test_entities.py
git commit -m "feat(claims): entity surface rules with pronoun and generic abstention

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Pure timeline rebuild

Spec §4.2 "Timelines" steps 1–5 and "Claim status".

**Files:**
- Create: `src/hermes_memory/timeline.py`
- Test: `tests/test_timeline.py`, `tests/property/test_timeline_properties.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Event(evidence_id: int, claim_id: int, span_id: int, event_time: datetime, speaker: str, act: str, object_key: str, polarity: str)`
  - `@dataclass(frozen=True) RowPlan(claim_id: int | None, object_key: str, polarity: str, status: str, valid_from: datetime, valid_to: datetime | None, evidence_ids: tuple[int, ...])`
  - `@dataclass(frozen=True) RebuildPlan(rows: tuple[RowPlan, ...], chain: tuple[int, ...], orphaned: tuple[int, ...])`. `chain` holds indexes into `rows` in timeline order; row `chain[i]` is superseded by row `chain[i+1]`.
  - `rebuild(events: Iterable[Event], existing_claim_ids: Iterable[int], *, functional: bool) -> RebuildPlan`
  - status constants `GROUNDED`, `UNCONFIRMED`, `RETRACTED`, `ORPHANED`

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/test_timeline.py
"""Timeline rebuild semantics (graph layer spec §4.2)."""
import itertools
import time
from datetime import datetime, timedelta, timezone

from hermes_memory.timeline import Event, rebuild

_ids = itertools.count(1)


def T(n: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)


def ev(t, obj, *, act="assert", speaker="user", pol="positive", claim=100, span=None):
    return Event(evidence_id=next(_ids), claim_id=claim, span_id=t if span is None else span,
                 event_time=T(t), speaker=speaker, act=act, object_key=obj, polarity=pol)


def grounded(plan):
    return [(r.object_key, r.valid_from, r.valid_to) for r in plan.rows if r.status == "grounded"]


def current(plan):
    return [(r.object_key, r.polarity) for r in plan.rows if r.status == "grounded" and r.valid_to is None]


def test_nyc_austin_nyc_is_three_intervals_and_two_nyc_rows():
    plan = rebuild([ev(1, "e:nyc"), ev(2, "e:austin"), ev(3, "e:nyc")], [], functional=True)
    assert grounded(plan) == [("e:nyc", T(1), T(2)), ("e:austin", T(2), T(3)), ("e:nyc", T(3), None)]
    assert plan.chain == (0, 1, 2)


def test_late_backfill_lands_in_its_interval():
    plan = rebuild([ev(1, "e:nyc"), ev(5, "e:nyc"), ev(3, "e:austin")], [], functional=True)
    assert grounded(plan) == [("e:nyc", T(1), T(3)), ("e:austin", T(3), T(5)), ("e:nyc", T(5), None)]


def test_repairing_newest_value_restores_previous_interval():
    plan = rebuild([ev(1, "e:nyc"), ev(2, "e:austin"), ev(3, "e:austin", act="retract")], [], functional=True)
    assert current(plan) == [("e:nyc", "positive")]
    assert [r.object_key for r in plan.rows if r.status == "retracted"] == ["e:austin"]


def test_repairing_middle_value_merges_neighbours():
    plan = rebuild([ev(1, "e:nyc"), ev(2, "e:austin"), ev(3, "e:nyc"), ev(4, "e:austin", act="retract")],
                   [], functional=True)
    assert grounded(plan) == [("e:nyc", T(1), None)]


def test_negative_assert_closes_current_value_without_replacement():
    plan = rebuild([ev(1, "e:austin"), ev(2, "e:austin", pol="negative")], [], functional=True)
    assert current(plan) == []
    assert grounded(plan) == [("e:austin", T(1), T(2))]


def test_negative_that_closes_nothing_is_a_point_row():
    plan = rebuild([ev(1, "e:austin"), ev(2, "e:paris", pol="negative")], [], functional=True)
    assert current(plan) == [("e:austin", "positive")]
    [point] = [r for r in plan.rows if r.object_key == "e:paris"]
    assert point.valid_from == point.valid_to == T(2)


def test_reassertion_after_retraction_is_current_again():
    plan = rebuild([ev(1, "e:austin"), ev(2, "e:austin", act="retract"), ev(3, "e:austin")], [], functional=True)
    assert current(plan) == [("e:austin", "positive")]


def test_non_functional_newer_polarity_wins():
    plan = rebuild([ev(1, "e:atlas"), ev(2, "e:atlas", pol="negative")], [], functional=False)
    assert current(plan) == [("e:atlas", "negative")]


def test_assistant_only_claim_is_unconfirmed():
    a = ev(1, "e:acme", speaker="assistant", claim=7)
    plan = rebuild([a], [7], functional=True)
    assert [(r.claim_id, r.status, r.valid_to) for r in plan.rows] == [(7, "unconfirmed", None)]


def test_user_confirmation_grounds_the_same_row_with_both_evidence():
    a = ev(1, "e:acme", speaker="assistant", claim=7)
    u = ev(2, "e:acme", claim=7)
    plan = rebuild([a, u], [7], functional=True)
    assert [(r.claim_id, r.status, r.evidence_ids) for r in plan.rows] == [
        (7, "grounded", (a.evidence_id, u.evidence_id))]


def test_user_retraction_of_assistant_claim_is_retracted():
    plan = rebuild([ev(1, "e:atlas", speaker="assistant"), ev(2, "e:atlas", act="retract")], [], functional=False)
    assert [r.status for r in plan.rows] == ["retracted"]


def test_rows_keep_identity_and_unused_rows_are_orphaned():
    plan = rebuild([ev(1, "e:nyc", claim=11), ev(2, "e:austin", claim=12)], [11, 12, 13], functional=True)
    assert [r.claim_id for r in plan.rows] == [11, 12]
    assert plan.orphaned == (13,)


def test_five_hundred_repeats_rebuild_quickly():
    events = [ev(i % 50, "e:nyc", span=i) for i in range(500)]
    started = time.perf_counter()
    plan = rebuild(events, [100], functional=True)
    assert time.perf_counter() - started < 0.2
    assert current(plan) == [("e:nyc", "positive")]
```

- [ ] **Step 2: Write the failing property tests**

```python
# tests/property/test_timeline_properties.py
"""Order independence and fixpoint of the timeline rebuild."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, strategies as st

from hermes_memory.timeline import Event, rebuild

CLAIMS = [101, 102, 103, 104]


def T(n):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)


@st.composite
def event_lists(draw, objects=("e:1", "e:2", "l:x")):
    n = draw(st.integers(0, 12))
    return [
        Event(
            evidence_id=i + 1,
            claim_id=draw(st.sampled_from(CLAIMS)),
            span_id=draw(st.integers(1, 6)),
            event_time=T(draw(st.integers(0, 8))),
            speaker=draw(st.sampled_from(["user", "user", "assistant"])),
            act=draw(st.sampled_from(["assert", "assert", "assert", "retract"])),
            object_key=draw(st.sampled_from(objects)),
            polarity=draw(st.sampled_from(["positive", "positive", "negative"])),
        )
        for i in range(n)
    ]


def _shape(plan):
    return [(r.object_key, r.polarity, r.status, r.valid_from, r.valid_to, r.evidence_ids) for r in plan.rows]


@settings(max_examples=300)
@given(st.data(), st.booleans())
def test_input_order_never_matters(data, functional):
    evs = data.draw(event_lists())
    shuffled = data.draw(st.permutations(evs))
    assert rebuild(evs, CLAIMS, functional=functional) == rebuild(shuffled, CLAIMS, functional=functional)


@settings(max_examples=300)
@given(event_lists(), st.booleans())
def test_every_event_lands_in_exactly_one_row(evs, functional):
    plan = rebuild(evs, CLAIMS, functional=functional)
    placed = [i for r in plan.rows for i in r.evidence_ids]
    assert sorted(placed) == sorted(e.evidence_id for e in evs)


@settings(max_examples=300)
@given(event_lists(), st.booleans())
def test_current_key_is_unique(evs, functional):
    plan = rebuild(evs, CLAIMS, functional=functional)
    current = [(r.object_key, r.polarity) for r in plan.rows
               if r.status in ("grounded", "unconfirmed") and r.valid_to is None]
    assert len(current) == len(set(current))
    if functional:
        assert sum(1 for r in plan.rows if r.status == "grounded" and r.valid_to is None) <= 1


@settings(max_examples=300)
@given(event_lists(objects=("e:1",)))
def test_non_functional_key_has_one_current_value_when_user_asserted(evs):
    plan = rebuild(evs, CLAIMS, functional=False)
    grounded_now = [r for r in plan.rows if r.status == "grounded" and r.valid_to is None]
    assert len(grounded_now) <= 1


@settings(max_examples=300)
@given(event_lists(), st.booleans())
def test_applying_a_plan_and_rebuilding_changes_nothing(evs, functional):
    first = rebuild(evs, CLAIMS, functional=functional)
    owner_of, next_id = {}, 1000
    for row in first.rows:
        cid = row.claim_id
        if cid is None:
            cid, next_id = next_id, next_id + 1
        for evidence_id in row.evidence_ids:
            owner_of[evidence_id] = cid
    moved = [replace(e, claim_id=owner_of[e.evidence_id]) for e in evs]
    existing = sorted(set(owner_of.values()) | set(first.orphaned))
    second = rebuild(moved, existing, functional=functional)
    assert _shape(second) == _shape(first)
    assert all(r.claim_id is not None for r in second.rows)
    assert set(second.orphaned) == set(first.orphaned)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_timeline.py tests/property/test_timeline_properties.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_memory.timeline'`

- [ ] **Step 4: Write the implementation**

```python
# src/hermes_memory/timeline.py
"""Timeline rebuild for one claim key (graph layer spec §4.2, "Timelines").

Pure and deterministic. The store loads a key's live evidence events, calls
``rebuild`` and applies the plan in the same transaction. Input order never
matters: every decision sorts by (event_time, span_id, evidence_id).

Functional keys vary by object; non-functional keys (one object) vary by
polarity. User retracts cancel the asserts of the same value at or before
them. Assistant asserts never shape intervals; they attach to one, or to an
unconfirmed row of their value.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

USER = "user"
ASSERT = "assert"
RETRACT = "retract"
POSITIVE = "positive"

GROUNDED = "grounded"
UNCONFIRMED = "unconfirmed"
RETRACTED = "retracted"
ORPHANED = "orphaned"


@dataclass(frozen=True)
class Event:
    evidence_id: int
    claim_id: int
    span_id: int
    event_time: datetime
    speaker: str
    act: str
    object_key: str
    polarity: str

    def order(self) -> tuple:
        return (self.event_time, self.span_id, self.evidence_id)

    def at(self) -> tuple:
        return (self.event_time, self.span_id)


@dataclass(frozen=True)
class RowPlan:
    claim_id: Optional[int]
    object_key: str
    polarity: str
    status: str
    valid_from: datetime
    valid_to: Optional[datetime]
    evidence_ids: tuple[int, ...]


@dataclass(frozen=True)
class RebuildPlan:
    rows: tuple[RowPlan, ...]
    chain: tuple[int, ...]
    orphaned: tuple[int, ...]


@dataclass
class _Draft:
    object_key: str
    polarity: str
    status: str
    start: datetime
    end: Optional[datetime]
    events: list


def rebuild(events: Iterable[Event], existing_claim_ids: Iterable[int], *, functional: bool) -> RebuildPlan:
    evs = sorted(events, key=Event.order)
    user_retracts = [e for e in evs if e.speaker == USER and e.act == RETRACT]

    def cancelled(e: Event) -> bool:
        return any(r.object_key == e.object_key and r.polarity == e.polarity and e.at() <= r.at()
                   for r in user_retracts)

    asserts = [e for e in evs if e.act == ASSERT]
    user_live = [e for e in asserts if e.speaker == USER and not cancelled(e)]
    other_live = [e for e in asserts if e.speaker != USER and not cancelled(e)]
    dead = [e for e in asserts if cancelled(e)]

    intervals: list[_Draft] = []
    points: list[_Draft] = []
    for e in user_live:
        cur = intervals[-1] if intervals and intervals[-1].end is None else None
        if functional and e.polarity != POSITIVE:
            if cur is not None and cur.object_key == e.object_key:
                cur.end = e.event_time
                cur.events.append(e)
            else:
                points.append(_Draft(e.object_key, e.polarity, GROUNDED, e.event_time, e.event_time, [e]))
            continue
        same_value = cur is not None and (
            cur.object_key == e.object_key if functional else cur.polarity == e.polarity)
        if same_value:
            cur.events.append(e)
            continue
        if cur is not None:
            cur.end = e.event_time
        intervals.append(_Draft(e.object_key, e.polarity, GROUNDED, e.event_time, None, [e]))

    unconfirmed: dict[tuple[str, str], _Draft] = {}
    for e in other_live:
        same = [d for d in intervals if d.object_key == e.object_key and d.polarity == e.polarity]
        home = next((d for d in same if d.start <= e.event_time and (d.end is None or e.event_time < d.end)), None)
        if home is None and same:
            home = same[-1]
        if home is not None:
            home.events.append(e)
            continue
        k = (e.object_key, e.polarity)
        if k in unconfirmed:
            unconfirmed[k].events.append(e)
        else:
            unconfirmed[k] = _Draft(e.object_key, e.polarity, UNCONFIRMED, e.event_time, None, [e])

    retracted: dict[tuple[str, str], _Draft] = {}
    for e in sorted(dead + [x for x in evs if x.act == RETRACT], key=Event.order):
        k = (e.object_key, e.polarity)
        if k in retracted:
            retracted[k].events.append(e)
            retracted[k].end = e.event_time
        else:
            retracted[k] = _Draft(e.object_key, e.polarity, RETRACTED, e.event_time, e.event_time, [e])

    drafts = intervals + points + [unconfirmed[k] for k in sorted(unconfirmed)] + [retracted[k] for k in sorted(retracted)]
    existing = sorted({int(c) for c in existing_claim_ids})
    unused = set(existing)
    rows: list[RowPlan] = []
    for d in drafts:
        votes = Counter(e.claim_id for e in d.events if e.claim_id in unused)
        claim_id = None
        if votes:
            top = max(votes.values())
            claim_id = min(c for c, n in votes.items() if n == top)
            unused.discard(claim_id)
        rows.append(RowPlan(claim_id, d.object_key, d.polarity, d.status, d.start, d.end,
                            tuple(sorted(e.evidence_id for e in d.events))))
    return RebuildPlan(tuple(rows), tuple(range(len(intervals))), tuple(c for c in existing if c in unused))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_timeline.py tests/property/test_timeline_properties.py -q`
Expected: PASS. If a property fails, hypothesis prints the minimal event list; fix `rebuild`, never the property.

- [ ] **Step 6: Commit**

```bash
git add src/hermes_memory/timeline.py tests/test_timeline.py tests/property/test_timeline_properties.py
git commit -m "feat(claims): pure event-time timeline rebuild

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: V18 schema migration

Spec §4.1 tables, §4.2 claims columns, verb table, evidence, current key, legacy columns; §7.2 step 1.

**Files:**
- Create: `sql/migrations/V18__claims_evidence.sql`
- Modify: `src/hermes_memory/schema_guard.py:84` (`CONSUMER_HEAD_VERSIONS`)
- Modify: `tests/conftest.py` (`clean_hermes_test_db`)
- Test: `tests/test_schema_guard.py`, `tests/integration/test_entities_db.py` (schema smoke)

**Interfaces:**
- Produces tables `entity`, `entity_alias`, `turn_entity`, `verb`, `verb_synonym`, `claim_evidence`, `data_migrations`; claims columns `owner, subject_id, object_id, fact_text, fact_vec, embed_model, status, valid_from, valid_to, legacy_valid`; indexes `uq_conversations_owner_id`, `uq_claims_owner_claim`, `claims_current_key`, `uq_claim_evidence_event`; trigger `trg_claims_valid_mirror`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_schema_guard.py`:

```python
def test_head_versions_include_claims_evidence() -> None:
    assert "V18" in CONSUMER_HEAD_VERSIONS
```

Create `tests/integration/test_entities_db.py` with the schema smoke test (Task 4 adds more to this file):

```python
"""Entities and V18 schema against real Postgres."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_v18_schema_is_present(db_pool, clean_hermes_test_db) -> None:
    async with db_pool.acquire() as conn:
        tables = {r["t"] for r in await conn.fetch(
            "SELECT table_name AS t FROM information_schema.tables WHERE table_schema = 'public'")}
        assert {"entity", "entity_alias", "turn_entity", "verb", "verb_synonym",
                "claim_evidence", "data_migrations"} <= tables
        verbs = {r["verb"]: r["functional"] for r in await conn.fetch("SELECT verb, functional FROM verb")}
        assert verbs["lives_in"] is True and verbs["uses"] is False
        assert await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'claims' AND column_name = 'span_id'") == "YES"
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_claim_live_key'") == 0


@pytest.mark.asyncio
async def test_valid_mirrors_status(db_pool, store, clean_hermes_test_db) -> None:
    span = await store.insert_turn("s", "alice", "user", "Mirror probe turn text.", None)
    async with db_pool.acquire() as conn:
        cid = await conn.fetchval(
            "INSERT INTO claims (owner, span_id, subject, verb, object, act, status) "
            "VALUES ('alice', $1, 'A', 'uses', 'B', 'assert', 'grounded') RETURNING claim_id", span)
        assert await conn.fetchval("SELECT valid FROM claims WHERE claim_id = $1", cid) == "live"
        await conn.execute("UPDATE claims SET valid_to = now() WHERE claim_id = $1", cid)
        assert await conn.fetchval("SELECT valid FROM claims WHERE claim_id = $1", cid) == "retracted"
        await conn.execute("UPDATE claims SET status = 'unconfirmed', valid_to = NULL WHERE claim_id = $1", cid)
        assert await conn.fetchval("SELECT valid FROM claims WHERE claim_id = $1", cid) == "unknown"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_schema_guard.py -q && pytest tests/integration/test_entities_db.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL (`"V18" in CONSUMER_HEAD_VERSIONS` is false; tables missing)

- [ ] **Step 3: Write the migration**

```sql
-- V18__claims_evidence.sql — owner-scoped entities, evidence and timelines
-- (graph layer spec v0.5.1 §4, §7.2 step 1). Claims become a projection of
-- claim_evidence; status replaces valid, which a trigger mirrors until Phase 4.
-- Legacy rows keep their pre-migration valid in legacy_valid and get a
-- provisional status; claims_migration.py (data_migrations 'claims_evidence_v1')
-- writes their evidence and rebuilds timelines. Idempotent: safe to re-run.

SET search_path = public;

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_owner_id ON conversations (agent_identity, id);

CREATE TABLE IF NOT EXISTS entity (
    owner TEXT NOT NULL,
    id BIGSERIAL NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'other'
        CHECK (type IN ('self', 'person', 'project', 'place', 'org', 'tool', 'concept', 'other')),
    name_vec vector(768),
    merged_into BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, id),
    UNIQUE (owner, name_norm, type),
    FOREIGN KEY (owner, merged_into) REFERENCES entity (owner, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_self ON entity (owner) WHERE type = 'self';
CREATE INDEX IF NOT EXISTS idx_entity_name_norm ON entity (owner, name_norm);

CREATE TABLE IF NOT EXISTS entity_alias (
    owner TEXT NOT NULL,
    surface_norm TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('equation', 'extractor', 'migration', 'manual')),
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retracted')),
    span_id BIGINT,
    event_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, surface_norm, entity_id),
    FOREIGN KEY (owner, entity_id) REFERENCES entity (owner, id),
    FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_alias_live ON entity_alias (owner, surface_norm) WHERE state = 'live';

CREATE TABLE IF NOT EXISTS turn_entity (
    owner TEXT NOT NULL,
    span_id BIGINT NOT NULL,
    entity_id BIGINT NOT NULL,
    surface TEXT NOT NULL,
    conf DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    PRIMARY KEY (owner, span_id, entity_id),
    FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
    FOREIGN KEY (owner, entity_id) REFERENCES entity (owner, id)
);
CREATE INDEX IF NOT EXISTS idx_turn_entity_entity ON turn_entity (owner, entity_id);

CREATE TABLE IF NOT EXISTS verb (
    verb TEXT PRIMARY KEY,
    functional BOOLEAN NOT NULL,
    phrase TEXT NOT NULL,
    neg_phrase TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS verb_synonym (
    phrase_norm TEXT PRIMARY KEY,
    verb TEXT NOT NULL REFERENCES verb (verb)
);
INSERT INTO verb (verb, functional, phrase, neg_phrase) VALUES
    ('is', false, 'is', 'is not'),
    ('uses', false, 'uses', 'does not use'),
    ('based_in', true, 'is based in', 'is not based in'),
    ('owned_by', true, 'is owned by', 'is not owned by'),
    ('works_for', true, 'works for', 'does not work for'),
    ('lives_in', true, 'lives in', 'does not live in'),
    ('role_is', true, 'has the role', 'does not have the role'),
    ('status_is', true, 'has the status', 'does not have the status'),
    ('works_on', false, 'works on', 'does not work on'),
    ('depends_on', false, 'depends on', 'does not depend on'),
    ('part_of', false, 'is part of', 'is not part of'),
    ('knows', false, 'knows', 'does not know'),
    ('likes', false, 'likes', 'does not like'),
    ('prefers', false, 'prefers', 'does not prefer')
ON CONFLICT (verb) DO NOTHING;
INSERT INTO verb_synonym (phrase_norm, verb) VALUES
    ('is', 'is'), ('uses', 'uses'), ('use', 'uses'),
    ('works for', 'works_for'), ('works at', 'works_for'), ('employed by', 'works_for'),
    ('is employed by', 'works_for'), ('lives in', 'lives_in'), ('based in', 'based_in'),
    ('is based in', 'based_in'), ('headquartered in', 'based_in'), ('owned by', 'owned_by'),
    ('is owned by', 'owned_by'), ('belongs to', 'owned_by'), ('role is', 'role_is'),
    ('status is', 'status_is'), ('works on', 'works_on'), ('depends on', 'depends_on'),
    ('part of', 'part_of'), ('is part of', 'part_of'), ('knows', 'knows'),
    ('likes', 'likes'), ('prefers', 'prefers')
ON CONFLICT (phrase_norm) DO NOTHING;

ALTER TABLE claims ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS subject_id BIGINT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS object_id BIGINT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS fact_text TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS fact_vec vector(768);
ALTER TABLE claims ADD COLUMN IF NOT EXISTS embed_model TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS legacy_valid TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_status_check') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_status_check
            CHECK (status IN ('grounded', 'unconfirmed', 'retracted', 'orphaned'));
    END IF;
END $$;

-- Freeze pre-migration truth once (new rows always carry an owner), then owner,
-- then a provisional status that matches valid until the data migration runs.
UPDATE claims SET legacy_valid = valid WHERE legacy_valid IS NULL AND owner IS NULL;
UPDATE claims c SET owner = cv.agent_identity
  FROM conversations cv WHERE cv.id = c.span_id AND c.owner IS NULL;
UPDATE claims SET status = CASE valid WHEN 'live' THEN 'grounded'
                                      WHEN 'retracted' THEN 'retracted'
                                      ELSE 'unconfirmed' END
 WHERE status IS NULL;

-- Provenance moves to claim_evidence; spans must be deletable.
ALTER TABLE claims ALTER COLUMN span_id DROP NOT NULL;
ALTER TABLE claims DROP CONSTRAINT IF EXISTS claims_span_id_fkey;

CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_owner_claim ON claims (owner, claim_id);
-- The global live key collides across owners; claims_current_key replaces it.
DROP INDEX IF EXISTS idx_claim_live_key;
CREATE UNIQUE INDEX IF NOT EXISTS claims_current_key ON claims (
    owner, subject_id, verb,
    (CASE WHEN object_id IS NOT NULL THEN 'e:' || object_id::text ELSE 'l:' || object_canon END),
    polarity)
  WHERE status IN ('grounded', 'unconfirmed') AND valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_claims_timeline ON claims (owner, subject_id, verb);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_subject_entity_fk') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_subject_entity_fk
            FOREIGN KEY (owner, subject_id) REFERENCES entity (owner, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_object_entity_fk') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_object_entity_fk
            FOREIGN KEY (owner, object_id) REFERENCES entity (owner, id);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION claims_valid_mirror() RETURNS trigger AS $$
BEGIN
    IF NEW.status IS NULL THEN
        RETURN NEW;
    END IF;
    NEW.valid := CASE
        WHEN NEW.status = 'grounded' AND NEW.valid_to IS NULL THEN 'live'
        WHEN NEW.status IN ('grounded', 'retracted') THEN 'retracted'
        ELSE 'unknown' END;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_claims_valid_mirror ON claims;
CREATE TRIGGER trg_claims_valid_mirror BEFORE INSERT OR UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_valid_mirror();

CREATE TABLE IF NOT EXISTS claim_evidence (
    owner TEXT NOT NULL,
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL,
    span_id BIGINT NOT NULL,
    speaker TEXT NOT NULL CHECK (speaker IN ('user', 'assistant')),
    act TEXT NOT NULL CHECK (act IN ('assert', 'retract')),
    polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    subject_surface TEXT NOT NULL,
    object_surface TEXT NOT NULL,
    subject_id BIGINT NOT NULL,
    verb TEXT NOT NULL REFERENCES verb (verb),
    object_key TEXT NOT NULL,
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    event_time TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'withdrawn', 'superseded_version')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (owner, claim_id) REFERENCES claims (owner, claim_id),
    FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
    FOREIGN KEY (owner, subject_id) REFERENCES entity (owner, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_evidence_event ON claim_evidence
    (owner, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act)
    WHERE state = 'live';
CREATE INDEX IF NOT EXISTS idx_claim_evidence_timeline
    ON claim_evidence (owner, subject_id, verb, object_key) WHERE state = 'live';
CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence (owner, claim_id);

CREATE TABLE IF NOT EXISTS data_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail JSONB NOT NULL DEFAULT '{}'
);
```

- [ ] **Step 4: Update the head versions and the truncate fixture**

In `src/hermes_memory/schema_guard.py` change line 84 to:

```python
CONSUMER_HEAD_VERSIONS = ("V1", "V9", "V10", "V11", "V16", "V17", "V18")
```

In `tests/conftest.py`, `clean_hermes_test_db`, replace both `TRUNCATE` statements with (claims no longer cascade from conversations, so list them explicitly; never truncate `verb` or `verb_synonym`, which hold seed rows):

```python
            """
            TRUNCATE conversations, memory_entries, memory_chunk_nodes,
                     doc_chunks, librarian_runs, semantic_edge, noun,
                     claims, claim_evidence, entity, entity_alias, turn_entity,
                     uptakes, aliases, data_migrations
                     RESTART IDENTITY CASCADE;
            """
```

- [ ] **Step 5: Run the tests to verify they pass, including idempotency**

Run: `pytest tests/test_schema_guard.py -q`
Expected: PASS
Run: `pytest tests/integration/test_entities_db.py tests/test_migrate_idempotent.py -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS (the idempotent re-run applies 0 migrations without error)

- [ ] **Step 6: Commit**

```bash
git add sql/migrations/V18__claims_evidence.sql src/hermes_memory/schema_guard.py tests/conftest.py tests/test_schema_guard.py tests/integration/test_entities_db.py
git commit -m "feat(claims): V18 entities, verbs, evidence and owner-scoped claims

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Entity resolution in the store

Spec §4.1 (self entity, merges followed on resolution, concurrent creation).

**Files:**
- Create: `src/hermes_memory/store_claims.py`
- Modify: `src/hermes_memory/store.py:271` (class bases)
- Test: `tests/integration/test_entities_db.py`

**Interfaces:**
- Consumes: Task 1 `normalize_name`, `is_abstained`, `is_first_person`, `SELF_NAME`, `SELF_NORM`
- Produces (module functions taking an asyncpg connection; later tasks and `claims_migration.py` import them):
  - `async def span_info(conn, span_id: int) -> SpanInfo` with `@dataclass(frozen=True) SpanInfo(owner: str, ts: datetime, role: str, session_id: str)`; raises `LookupError` when missing
  - `async def resolve_entity(conn, owner: str, surface: str) -> int | None`
  - `async def ensure_entity(conn, owner: str, surface: str, type_: str = "other") -> int`; raises `ValueError` for an abstained surface
  - `async def follow_merges(conn, owner: str, entity_id: int) -> int`
- Produces (`StoreClaimsMixin` methods): `async def ensure_entity(self, owner, surface, type_="other") -> int`, `async def resolve_entity(self, owner, surface) -> int | None`

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/test_entities_db.py`)

```python
import asyncio


@pytest.mark.asyncio
async def test_concurrent_creation_of_one_name_makes_one_entity(db_pool, store, clean_hermes_test_db) -> None:
    ids = await asyncio.gather(*[store.ensure_entity("alice", "Acme Corp") for _ in range(6)])
    assert len(set(ids)) == 1
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM entity WHERE owner = 'alice'") == 1


@pytest.mark.asyncio
async def test_same_name_is_two_entities_for_two_owners(store, clean_hermes_test_db) -> None:
    assert await store.ensure_entity("alice", "Acme") != await store.ensure_entity("bob", "Acme")


@pytest.mark.asyncio
async def test_first_person_resolves_to_one_self_entity(db_pool, store, clean_hermes_test_db) -> None:
    a = await store.ensure_entity("alice", "I")
    b = await store.ensure_entity("alice", "me")
    assert a == b
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT type FROM entity WHERE owner = 'alice' AND id = $1", a) == "self"


@pytest.mark.asyncio
async def test_abstained_surface_never_creates_an_entity(store, clean_hermes_test_db) -> None:
    with pytest.raises(ValueError):
        await store.ensure_entity("alice", "the project")
    assert await store.resolve_entity("alice", "it") is None


@pytest.mark.asyncio
async def test_resolution_follows_merges(db_pool, store, clean_hermes_test_db) -> None:
    old = await store.ensure_entity("alice", "Tokyo Eye")
    new = await store.ensure_entity("alice", "TE Project")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE entity SET merged_into = $2 WHERE owner = 'alice' AND id = $1", new, old)
    assert await store.resolve_entity("alice", "TE Project") == old
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_entities_db.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'ensure_entity'`

- [ ] **Step 3: Write the implementation**

```python
# src/hermes_memory/store_claims.py
"""Owner-scoped entities, claim evidence and timelines (graph layer spec §4, §5.3).

Module functions take an asyncpg connection so one transaction can compose
them (reception, repair, merges, deletion, the legacy data migration).
StoreClaimsMixin wraps them for callers that hold a pool.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .entities import SELF_NAME, SELF_NORM, is_abstained, is_first_person, normalize_name

logger = logging.getLogger("hermes_memory.store_claims")


@dataclass(frozen=True)
class SpanInfo:
    owner: str
    ts: datetime
    role: str
    session_id: str


async def span_info(conn, span_id: int) -> SpanInfo:
    row = await conn.fetchrow(
        "SELECT agent_identity, ts, role, session_id FROM conversations WHERE id = $1", int(span_id))
    if row is None:
        raise LookupError(f"span {span_id} not found")
    return SpanInfo(row["agent_identity"], row["ts"], row["role"], row["session_id"])


async def follow_merges(conn, owner: str, entity_id: int) -> int:
    cur = int(entity_id)
    for _ in range(16):
        nxt = await conn.fetchval("SELECT merged_into FROM entity WHERE owner = $1 AND id = $2", owner, cur)
        if nxt is None:
            return cur
        cur = int(nxt)
    logger.warning("merge chain longer than 16 for owner=%s entity=%s", owner, entity_id)
    return cur


async def resolve_entity(conn, owner: str, surface: str) -> Optional[int]:
    if is_first_person(surface):
        sid = await conn.fetchval("SELECT id FROM entity WHERE owner = $1 AND type = 'self'", owner)
        return int(sid) if sid is not None else None
    if is_abstained(surface):
        return None
    norm = normalize_name(surface)
    eid = await conn.fetchval(
        "SELECT entity_id FROM entity_alias WHERE owner = $1 AND surface_norm = $2 AND state = 'live' "
        "ORDER BY created_at, entity_id LIMIT 1", owner, norm)
    if eid is None:
        eid = await conn.fetchval(
            "SELECT id FROM entity WHERE owner = $1 AND name_norm = $2 ORDER BY id LIMIT 1", owner, norm)
    return None if eid is None else await follow_merges(conn, owner, int(eid))


async def ensure_entity(conn, owner: str, surface: str, type_: str = "other") -> int:
    found = await resolve_entity(conn, owner, surface)
    if found is not None:
        return found
    if is_first_person(surface):
        name, norm, type_ = SELF_NAME, SELF_NORM, "self"
    elif is_abstained(surface):
        raise ValueError(f"abstained surface cannot name an entity: {surface!r}")
    else:
        name, norm = surface.strip(), normalize_name(surface)
    new_id = await conn.fetchval(
        "INSERT INTO entity (owner, name, name_norm, type) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT DO NOTHING RETURNING id", owner, name, norm, type_)
    if new_id is not None:
        return int(new_id)
    if type_ == "self":
        got = await conn.fetchval("SELECT id FROM entity WHERE owner = $1 AND type = 'self'", owner)
    else:
        got = await conn.fetchval(
            "SELECT id FROM entity WHERE owner = $1 AND name_norm = $2 AND type = $3", owner, norm, type_)
    return await follow_merges(conn, owner, int(got))


class StoreClaimsMixin:
    """Pool-holding wrappers over the module functions."""

    pool: object

    async def ensure_entity(self, owner: str, surface: str, type_: str = "other") -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await ensure_entity(conn, owner, surface, type_)

    async def resolve_entity(self, owner: str, surface: str) -> Optional[int]:
        async with self.pool.acquire() as conn:
            return await resolve_entity(conn, owner, surface)
```

In `src/hermes_memory/store.py`, import the mixin next to the other mixin imports (line 38–40) and add it to the class bases:

```python
from .store_claims import StoreClaimsMixin
...
class Store(StoreExpandMixin, StoreMergeMixin, StoreConceptsMixin, StoreClaimsMixin):
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/test_entities_db.py -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store_claims.py src/hermes_memory/store.py tests/integration/test_entities_db.py
git commit -m "feat(claims): owner-scoped entity resolution with race-safe creation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Evidence and timeline application

Spec §4.2 (evidence, idempotency key, timelines, placeholder rows, `fact_text`), §5.2 step 3 in its Phase 1 inline form.

**Files:**
- Modify: `src/hermes_memory/store_claims.py`
- Create: `tests/integration/claims_helpers.py`, `tests/integration/test_claims_timeline.py`

**Interfaces:**
- Consumes: Task 2 `Event`, `rebuild`; Task 4 functions
- Produces:
  - constants `RULES_EXTRACTOR = "rules"`, `RULES_VERSION = "1"`, `OBJECT_KEY_SQL`
  - `@dataclass(frozen=True) TimelineKey(owner: str, subject_id: int, verb: str, object_key: str | None)` (`object_key` is None for functional verbs); `def sort_key(k) -> tuple`
  - `async def verb_row(conn, verb_or_phrase: str) -> asyncpg.Record | None` (columns `verb, functional, phrase, neg_phrase`)
  - `def timeline_key(owner, subject_id, verb_rec, object_key) -> TimelineKey`
  - `async def object_key_for(conn, owner, surface) -> str | None`
  - `async def placeholder_claim(conn, owner, subject_id, verb, object_key, polarity, span_id) -> int`
  - `async def add_evidence(conn, info: SpanInfo, span_id, claim: dict, *, act="assert", extractor=RULES_EXTRACTOR, version=RULES_VERSION) -> tuple[int, TimelineKey] | None`
  - `async def lock_timeline(conn, owner, subject_id, verb) -> None` (advisory xact lock; every evidence writer takes it before placing evidence)
  - `async def rebuild_key(conn, key: TimelineKey) -> None`; `async def rebuild_keys(conn, keys) -> None` (sorted order)
  - mixin: `async def record_rule_claims(self, span_id: int, claims: Sequence[dict], *, extractor=RULES_EXTRACTOR, version=RULES_VERSION) -> list[int]`. `claims` items: `{"subject", "verb", "object", "polarity"?}`; returns the claim id each kept item's evidence ended on, in input order, skipping abstained or unknown-verb items.

- [ ] **Step 1: Write the test helpers**

```python
# tests/integration/claims_helpers.py
"""Shared helpers for claims integration tests."""
from __future__ import annotations

import itertools

_n = itertools.count()


def t(minute: int) -> str:
    return f"2026-01-01T00:{minute:02d}:00+00:00"


async def span(store, owner: str, minute: int, role: str = "user", session: str = "s1") -> int:
    tid = await store.insert_turn(session, owner, role, f"turn {next(_n)} at {minute}", None, ts=t(minute))
    return int(tid)


async def current(db_pool, owner: str, subject_norm: str, verb: str) -> list[tuple[str, str]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.object, c.polarity FROM claims c JOIN entity s ON s.owner = c.owner AND s.id = c.subject_id "
            "WHERE c.owner = $1 AND s.name_norm = $2 AND c.verb = $3 AND c.status = 'grounded' "
            "AND c.valid_to IS NULL ORDER BY c.claim_id", owner, subject_norm, verb)
    return [(r["object"], r["polarity"]) for r in rows]


async def history(db_pool, owner: str, subject_norm: str, verb: str) -> list[tuple[str, str, bool]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.object, c.status, c.valid_to IS NULL AS is_current FROM claims c "
            "JOIN entity s ON s.owner = c.owner AND s.id = c.subject_id "
            "WHERE c.owner = $1 AND s.name_norm = $2 AND c.verb = $3 AND c.status = 'grounded' "
            "ORDER BY c.valid_from", owner, subject_norm, verb)
    return [(r["object"], r["status"], r["is_current"]) for r in rows]
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/integration/test_claims_timeline.py
"""Evidence-derived claims and event-time timelines (spec §4.2, §9 timelines gate)."""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.claims_helpers import current, history, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


def lives(obj, pol="positive"):
    return {"subject": "Raul", "verb": "lives in", "object": obj, "polarity": pol}


@pytest.mark.asyncio
async def test_rule_claim_becomes_grounded_with_fact_text_and_turn_entities(db_pool, store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    [cid] = await store.record_rule_claims(s, [{"subject": "Nightingale", "verb": "uses", "object": "Atlas"}])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, valid_to, fact_text, valid FROM claims WHERE claim_id = $1", cid)
        assert (row["status"], row["valid_to"], row["valid"]) == ("grounded", None, "live")
        assert row["fact_text"] == "Nightingale uses Atlas"
        assert await conn.fetchval("SELECT count(*) FROM turn_entity WHERE span_id = $1", s) == 2


@pytest.mark.asyncio
async def test_reprocessing_a_span_adds_no_evidence(db_pool, store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    claim = [{"subject": "Nightingale", "verb": "uses", "object": "Atlas"}]
    first = await store.record_rule_claims(s, claim)
    second = await store.record_rule_claims(s, claim)
    assert first == second
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM claim_evidence") == 1


@pytest.mark.asyncio
async def test_abstained_subject_and_unknown_verb_are_skipped(store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    assert await store.record_rule_claims(s, [
        {"subject": "It", "verb": "uses", "object": "Atlas"},
        {"subject": "Raul", "verb": "juggles", "object": "Atlas"},
    ]) == []


@pytest.mark.asyncio
async def test_nyc_austin_nyc_timeline(db_pool, store, clean_hermes_test_db):
    for minute, city in [(1, "NYC"), (2, "Austin"), (3, "NYC")]:
        await store.record_rule_claims(await span(store, "alice", minute), [lives(city)])
    assert await history(db_pool, "alice", "raul", "lives_in") == [
        ("NYC", "grounded", False), ("Austin", "grounded", False), ("NYC", "grounded", True)]


@pytest.mark.asyncio
async def test_out_of_order_backfill_lands_in_its_interval(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [lives("NYC")])
    await store.record_rule_claims(await span(store, "alice", 5), [lives("NYC")])
    await store.record_rule_claims(await span(store, "alice", 3), [lives("Austin")])
    assert [o for o, _, _ in await history(db_pool, "alice", "raul", "lives_in")] == ["NYC", "Austin", "NYC"]
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]


@pytest.mark.asyncio
async def test_contradictory_polarity_resolves_to_the_newer_statement(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1),
                                   [{"subject": "Nightingale", "verb": "uses", "object": "Atlas"}])
    await store.record_rule_claims(await span(store, "alice", 2),
                                   [{"subject": "Nightingale", "verb": "uses", "object": "Atlas", "polarity": "negative"}])
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "negative")]


@pytest.mark.asyncio
async def test_owners_never_share_claims(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [lives("NYC")])
    await store.record_rule_claims(await span(store, "bob", 2), [lives("Austin")])
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]
    assert await current(db_pool, "bob", "raul", "lives_in") == [("Austin", "positive")]


@pytest.mark.asyncio
async def test_concurrent_writers_leave_one_current_row(db_pool, store, clean_hermes_test_db):
    spans = [await span(store, "alice", m) for m in range(1, 7)]
    await asyncio.gather(*[store.record_rule_claims(s, [lives("NYC" if i % 2 else "Austin")])
                           for i, s in enumerate(spans)])
    assert len(await current(db_pool, "alice", "raul", "lives_in")) == 1
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/integration/test_claims_timeline.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'record_rule_claims'`

- [ ] **Step 4: Write the implementation** (append to `src/hermes_memory/store_claims.py`; add `import asyncpg`, `from typing import Iterable, Sequence`, `from .entities import classify_object` and `from .timeline import Event, rebuild` to the imports)

```python
RULES_EXTRACTOR = "rules"
RULES_VERSION = "1"
OBJECT_KEY_SQL = "(CASE WHEN object_id IS NOT NULL THEN 'e:' || object_id::text ELSE 'l:' || object_canon END)"
_SPEAKERS = {"user": "user", "assistant": "assistant"}


@dataclass(frozen=True)
class TimelineKey:
    owner: str
    subject_id: int
    verb: str
    object_key: Optional[str]


def sort_key(k: TimelineKey) -> tuple:
    return (k.owner, k.subject_id, k.verb, k.object_key or "")


async def lock_timeline(conn, owner: str, subject_id: int, verb: str) -> None:
    """Serialize every writer of one (owner, subject, verb) for this transaction.

    Taken before placing evidence as well as before the rebuild, so no writer
    can point evidence at a placeholder another writer is about to delete.
    Re-entrant within a transaction.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                       f"timeline|{owner}|{int(subject_id)}|{verb}")


async def verb_row(conn, verb_or_phrase: str):
    raw = (verb_or_phrase or "").strip().lower()
    row = await conn.fetchrow("SELECT verb, functional, phrase, neg_phrase FROM verb WHERE verb = $1", raw)
    if row is None:
        row = await conn.fetchrow(
            "SELECT v.verb, v.functional, v.phrase, v.neg_phrase FROM verb_synonym s "
            "JOIN verb v ON v.verb = s.verb WHERE s.phrase_norm = $1", normalize_name(raw))
    return row


def timeline_key(owner: str, subject_id: int, verb_rec, object_key: str) -> TimelineKey:
    return TimelineKey(owner, int(subject_id), verb_rec["verb"], None if verb_rec["functional"] else object_key)


async def object_key_for(conn, owner: str, surface: str) -> Optional[str]:
    kind = classify_object(surface)
    if kind == "abstain":
        return None
    if kind == "entity":
        return f"e:{await ensure_entity(conn, owner, surface)}"
    canon = normalize_name(surface)
    return f"l:{canon}" if canon else None


async def _object_fields(conn, owner: str, object_key: str, surfaces: Sequence[str]) -> tuple:
    if object_key.startswith("e:"):
        oid = int(object_key[2:])
        ent = await conn.fetchrow("SELECT name, name_norm FROM entity WHERE owner = $1 AND id = $2", owner, oid)
        return oid, ent["name_norm"], ent["name"]
    canon = object_key[2:]
    text = next((s for s in reversed(surfaces) if s), canon)
    return None, canon, text


_INSERT_CLAIM = """
INSERT INTO claims (owner, span_id, subject, verb, object, polarity, act, uptake,
                    subject_canon, object_canon, subject_id, object_id, status,
                    valid_from, valid_to, fact_text)
VALUES ($1, $2, $3, $4, $5, $6, 'assert', 'unknown', $7, $8, $9, $10, $11, $12, $13, $14)
RETURNING claim_id
"""
_UPDATE_CLAIM = """
UPDATE claims SET owner = $2, span_id = $3, subject = $4, verb = $5, object = $6, polarity = $7,
                  subject_canon = $8, object_canon = $9, subject_id = $10, object_id = $11,
                  status = $12, valid_from = $13, valid_to = $14, fact_text = $15
 WHERE claim_id = $1
"""


async def placeholder_claim(conn, owner: str, subject_id: int, verb: str, object_key: str,
                            polarity: str, span_id: int) -> int:
    """A row for new evidence to point at; the rebuild assigns real rows."""
    found = await conn.fetchval(
        f"SELECT claim_id FROM claims WHERE owner = $1 AND subject_id = $2 AND verb = $3 "
        f"AND {OBJECT_KEY_SQL} = $4 AND polarity = $5 "
        f"ORDER BY (status = 'grounded' AND valid_to IS NULL) DESC, claim_id LIMIT 1",
        owner, subject_id, verb, object_key, polarity)
    if found is not None:
        return int(found)
    subj = await conn.fetchrow("SELECT name, name_norm FROM entity WHERE owner = $1 AND id = $2", owner, subject_id)
    oid, canon, text = await _object_fields(conn, owner, object_key, [])
    return int(await conn.fetchval(
        _INSERT_CLAIM, owner, span_id, subj["name"], verb, text, polarity, subj["name_norm"], canon,
        subject_id, oid, "orphaned", None, None, None))


async def add_evidence(conn, info: SpanInfo, span_id: int, claim: dict, *, act: str = "assert",
                       extractor: str = RULES_EXTRACTOR, version: str = RULES_VERSION):
    speaker = _SPEAKERS.get(info.role)
    subject, obj = str(claim.get("subject") or ""), str(claim.get("object") or "")
    if speaker is None or is_abstained(subject):
        return None
    vrec = await verb_row(conn, str(claim.get("verb") or ""))
    if vrec is None:
        logger.info("unknown verb skipped: %r", claim.get("verb"))
        return None
    object_key = await object_key_for(conn, info.owner, obj)
    if object_key is None:
        return None
    subject_id = await ensure_entity(conn, info.owner, subject)
    await lock_timeline(conn, info.owner, subject_id, vrec["verb"])
    polarity = claim.get("polarity") or "positive"
    holder = await placeholder_claim(conn, info.owner, subject_id, vrec["verb"], object_key, polarity, span_id)
    ev_id = await conn.fetchval(
        "INSERT INTO claim_evidence (owner, claim_id, span_id, speaker, act, polarity, subject_surface, "
        "object_surface, subject_id, verb, object_key, extractor, extractor_version, event_time) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
        "ON CONFLICT (owner, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act) "
        "WHERE state = 'live' DO NOTHING RETURNING id",
        info.owner, holder, int(span_id), speaker, act, polarity, subject, obj, subject_id, vrec["verb"],
        object_key, extractor, version, claim.get("event_time") or info.ts)
    if ev_id is None:
        ev_id = await conn.fetchval(
            "SELECT id FROM claim_evidence WHERE owner = $1 AND span_id = $2 AND extractor = $3 "
            "AND extractor_version = $4 AND subject_id = $5 AND verb = $6 AND object_key = $7 "
            "AND polarity = $8 AND act = $9 AND state = 'live'",
            info.owner, int(span_id), extractor, version, subject_id, vrec["verb"], object_key, polarity, act)
    return int(ev_id), timeline_key(info.owner, subject_id, vrec, object_key)


async def rebuild_key(conn, key: TimelineKey) -> None:
    vrec = await verb_row(conn, key.verb)
    await lock_timeline(conn, key.owner, key.subject_id, key.verb)
    args: list = [key.owner, key.subject_id, key.verb]
    ev_filter = row_filter = ""
    if key.object_key is not None:
        args.append(key.object_key)
        ev_filter, row_filter = " AND object_key = $4", f" AND {OBJECT_KEY_SQL} = $4"
    ev_rows = await conn.fetch(
        "SELECT id, claim_id, span_id, event_time, speaker, act, object_key, polarity, object_surface "
        "FROM claim_evidence WHERE owner = $1 AND subject_id = $2 AND verb = $3 AND state = 'live'" + ev_filter,
        *args)
    existing = [int(r["claim_id"]) for r in await conn.fetch(
        "SELECT claim_id FROM claims WHERE owner = $1 AND subject_id = $2 AND verb = $3" + row_filter, *args)]
    events = [Event(int(r["id"]), int(r["claim_id"]), int(r["span_id"]), r["event_time"], r["speaker"],
                    r["act"], r["object_key"], r["polarity"]) for r in ev_rows]
    plan = rebuild(events, existing, functional=bool(vrec["functional"]))
    surface = {int(r["id"]): r["object_surface"] for r in ev_rows}
    span_of = {int(r["id"]): int(r["span_id"]) for r in ev_rows}
    subj = await conn.fetchrow("SELECT name, name_norm FROM entity WHERE owner = $1 AND id = $2",
                               key.owner, key.subject_id)
    if existing:
        await conn.execute("UPDATE claims SET status = 'orphaned', superseded_by = NULL "
                           "WHERE claim_id = ANY($1::bigint[])", existing)
    ids: list[int] = []
    for row in plan.rows:
        oid, canon, text = await _object_fields(conn, key.owner, row.object_key,
                                                [surface[i] for i in row.evidence_ids])
        phrase = vrec["phrase"] if row.polarity == "positive" else vrec["neg_phrase"]
        values = (key.owner, max(span_of[i] for i in row.evidence_ids), subj["name"], key.verb, text,
                  row.polarity, subj["name_norm"], canon, key.subject_id, oid, row.status,
                  row.valid_from, row.valid_to, f"{subj['name']} {phrase} {text}")
        if row.claim_id is None:
            cid = int(await conn.fetchval(_INSERT_CLAIM, *values))
        else:
            cid = row.claim_id
            await conn.execute(_UPDATE_CLAIM, cid, *values)
        await conn.execute("UPDATE claim_evidence SET claim_id = $1 WHERE id = ANY($2::bigint[])",
                           cid, list(row.evidence_ids))
        ids.append(cid)
    for a, b in zip(plan.chain, plan.chain[1:]):
        await conn.execute("UPDATE claims SET superseded_by = $1 WHERE claim_id = $2", ids[b], ids[a])
    if plan.orphaned:
        await conn.execute(
            "DELETE FROM claims c WHERE c.claim_id = ANY($1::bigint[]) AND c.legacy_valid IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.owner = c.owner AND e.claim_id = c.claim_id) "
            "AND NOT EXISTS (SELECT 1 FROM claims s WHERE s.superseded_by = c.claim_id)",
            list(plan.orphaned))


async def rebuild_keys(conn, keys: Iterable[TimelineKey]) -> None:
    for key in sorted(set(keys), key=sort_key):
        await rebuild_key(conn, key)


async def _write_turn_entities(conn, owner: str, span_id: int, evidence_ids: Sequence[int]) -> None:
    rows = await conn.fetch(
        "SELECT subject_id, subject_surface, object_key, object_surface FROM claim_evidence "
        "WHERE id = ANY($1::bigint[])", list(evidence_ids))
    for r in rows:
        pairs = [(int(r["subject_id"]), r["subject_surface"])]
        if r["object_key"].startswith("e:"):
            pairs.append((int(r["object_key"][2:]), r["object_surface"]))
        for entity_id, surface in pairs:
            await conn.execute(
                "INSERT INTO turn_entity (owner, span_id, entity_id, surface) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT DO NOTHING", owner, int(span_id), entity_id, surface)
```

Add to `StoreClaimsMixin`:

```python
    async def record_rule_claims(self, span_id: int, claims: Sequence[dict], *,
                                 extractor: str = RULES_EXTRACTOR, version: str = RULES_VERSION) -> list[int]:
        """Publish one span's extracted claims as evidence and rebuild their timelines.

        One transaction (spec §5.2 step 3, Phase 1 inline form). Abstained
        surfaces and unknown verbs are skipped; a re-run adds nothing. Two spans
        naming the same keys in opposite order can deadlock on the timeline
        locks; Postgres aborts one and it is retried.
        """
        for attempt in range(3):
            try:
                return await self._record_rule_claims_once(span_id, claims, extractor, version)
            except asyncpg.exceptions.DeadlockDetectedError:
                if attempt == 2:
                    raise
        return []

    async def _record_rule_claims_once(self, span_id, claims, extractor, version) -> list[int]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                info = await span_info(conn, span_id)
                added = [await add_evidence(conn, info, span_id, c, extractor=extractor, version=version)
                         for c in claims]
                kept = [a for a in added if a is not None]
                if not kept:
                    return []
                await rebuild_keys(conn, [k for _, k in kept])
                ev_ids = [e for e, _ in kept]
                await _write_turn_entities(conn, info.owner, span_id, ev_ids)
                owner_of = {int(r["id"]): int(r["claim_id"]) for r in await conn.fetch(
                    "SELECT id, claim_id FROM claim_evidence WHERE id = ANY($1::bigint[])", ev_ids)}
                return [owner_of[e] for e in ev_ids]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/integration/test_claims_timeline.py -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS (8 tests). If `test_concurrent_writers_leave_one_current_row` fails, check that `add_evidence` takes `lock_timeline` before `placeholder_claim`, that `rebuild_keys` goes in `sort_key` order, and that `placeholder_claim` never inserts a `grounded` row.

- [ ] **Step 6: Commit**

```bash
git add src/hermes_memory/store_claims.py tests/integration/claims_helpers.py tests/integration/test_claims_timeline.py
git commit -m "feat(claims): evidence writes with locked event-time timeline rebuild

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Repair writes retract evidence

Spec §5.3 (repair handling), §4.2 step 2 (corrections).

**Files:**
- Modify: `src/hermes_memory/store_claims.py`
- Create: `tests/integration/test_claims_repair.py`

**Interfaces:**
- Consumes: Task 5 functions
- Produces: mixin `async def record_repair(self, prior_span_id: int, repair_span_id: int, *, deictic: bool, new_claims: Sequence[dict] = ()) -> int` (retract evidence rows written)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_claims_repair.py
"""Repairs retract the claims they contradict (spec §5.3)."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import span

pytestmark = [pytest.mark.store, pytest.mark.integration]
ATLAS = {"subject": "Nightingale", "verb": "uses", "object": "Atlas"}


async def _status(db_pool, cid):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", cid)


@pytest.mark.asyncio
async def test_assistant_claim_is_unconfirmed(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    [cid] = await store.record_rule_claims(a, [ATLAS])
    assert await _status(db_pool, cid) == "unconfirmed"


@pytest.mark.asyncio
async def test_deictic_repair_retracts_the_prior_spans_claims(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    [cid] = await store.record_rule_claims(a, [ATLAS])
    u = await span(store, "alice", 2)
    assert await store.record_repair(a, u, deictic=True) == 1
    assert await _status(db_pool, cid) == "retracted"


@pytest.mark.asyncio
async def test_opposite_polarity_repair_retracts_only_the_contradicted_claim(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    atlas, borealis = await store.record_rule_claims(
        a, [ATLAS, {"subject": "Nightingale", "verb": "uses", "object": "Borealis"}])
    u = await span(store, "alice", 2)
    negated = [{**ATLAS, "polarity": "negative"}]
    assert await store.record_repair(a, u, deictic=False, new_claims=negated) == 1
    assert await _status(db_pool, atlas) == "retracted"
    assert await _status(db_pool, borealis) == "unconfirmed"


@pytest.mark.asyncio
async def test_repair_never_crosses_owners(store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    await store.record_rule_claims(a, [ATLAS])
    u = await span(store, "bob", 2)
    assert await store.record_repair(a, u, deictic=True) == 0


@pytest.mark.asyncio
async def test_repeated_repair_is_idempotent(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    await store.record_rule_claims(a, [ATLAS])
    u = await span(store, "alice", 2)
    await store.record_repair(a, u, deictic=True)
    await store.record_repair(a, u, deictic=True)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM claim_evidence WHERE act = 'retract'") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_claims_repair.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'record_repair'` (the first test passes already)

- [ ] **Step 3: Write the implementation** (append to `StoreClaimsMixin`)

```python
    async def record_repair(self, prior_span_id: int, repair_span_id: int, *, deictic: bool,
                            new_claims: Sequence[dict] = ()) -> int:
        """User retract evidence for the prior span's claims this repair contradicts.

        Deictic: every claim the prior span asserted. Otherwise: prior-span
        claims with the same subject, verb and object as a new claim and the
        opposite polarity. Same owner only; idempotent by the evidence key.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                info = await span_info(conn, repair_span_id)
                prior = await span_info(conn, prior_span_id)
                if info.role != "user" or prior.owner != info.owner:
                    return 0
                rows = await conn.fetch(
                    "SELECT DISTINCT subject_id, verb, object_key, polarity, subject_surface, object_surface "
                    "FROM claim_evidence WHERE owner = $1 AND span_id = $2 AND state = 'live' AND act = 'assert'",
                    info.owner, int(prior_span_id))
                if not deictic:
                    contradicted = set()
                    for c in new_claims:
                        sid = await resolve_entity(conn, info.owner, str(c.get("subject") or ""))
                        vrec = await verb_row(conn, str(c.get("verb") or ""))
                        okey = await object_key_for(conn, info.owner, str(c.get("object") or ""))
                        if sid is None or vrec is None or okey is None:
                            continue
                        flipped = "negative" if (c.get("polarity") or "positive") == "positive" else "positive"
                        contradicted.add((sid, vrec["verb"], okey, flipped))
                    rows = [r for r in rows if (int(r["subject_id"]), r["verb"], r["object_key"], r["polarity"])
                            in contradicted]
                written, keys = 0, []
                for r in rows:
                    vrec = await verb_row(conn, r["verb"])
                    await lock_timeline(conn, info.owner, int(r["subject_id"]), r["verb"])
                    holder = await placeholder_claim(conn, info.owner, int(r["subject_id"]), r["verb"],
                                                     r["object_key"], r["polarity"], repair_span_id)
                    ev = await conn.fetchval(
                        "INSERT INTO claim_evidence (owner, claim_id, span_id, speaker, act, polarity, "
                        "subject_surface, object_surface, subject_id, verb, object_key, extractor, "
                        "extractor_version, event_time) VALUES ($1, $2, $3, 'user', 'retract', $4, $5, $6, $7, "
                        "$8, $9, $10, $11, $12) ON CONFLICT (owner, span_id, extractor, extractor_version, "
                        "subject_id, verb, object_key, polarity, act) WHERE state = 'live' DO NOTHING RETURNING id",
                        info.owner, holder, int(repair_span_id), r["polarity"], r["subject_surface"],
                        r["object_surface"], int(r["subject_id"]), r["verb"], r["object_key"],
                        RULES_EXTRACTOR, RULES_VERSION, info.ts)
                    if ev is not None:
                        written += 1
                    keys.append(timeline_key(info.owner, int(r["subject_id"]), vrec, r["object_key"]))
                await rebuild_keys(conn, keys)
                return written
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/test_claims_repair.py -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store_claims.py tests/integration/test_claims_repair.py
git commit -m "feat(claims): repairs write user retract evidence

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Alias equations, merges and re-resolution

Spec §4.1 (`entity_alias`, merges, un-merge), §4.2 (re-resolution from stored surfaces).

**Files:**
- Modify: `src/hermes_memory/store_claims.py`
- Create: `tests/integration/test_claims_aliases.py`

**Interfaces:**
- Produces:
  - `async def upsert_alias(conn, owner, surface_norm, entity_id, source, span_id, event_time) -> None`
  - `async def reresolve(conn, owner, entity_ids: set[int]) -> set[TimelineKey]`
  - `async def unmerge_unjustified(conn, owner) -> set[TimelineKey]`
  - mixin `async def record_alias_equation(self, span_id: int, canon_surface: str, alias_surface: str) -> bool`
- Merge justification: entity M merged into S stays merged while a live `equation` or `manual` alias row `(owner, M.name_norm, S)` exists. Restating an equation moves that row's span to the newer span (documented in the code comment).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_claims_aliases.py
"""Alias equations attach surfaces and merge entities (spec §4.1)."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


@pytest.mark.asyncio
async def test_equation_makes_alias_resolve_to_canon(store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    assert await store.record_alias_equation(s, "Tokyo Eye", "TE")
    assert await store.resolve_entity("alice", "TE") == await store.resolve_entity("alice", "Tokyo Eye")


@pytest.mark.asyncio
async def test_claims_about_alias_land_on_the_canon_entity(db_pool, store, clean_hermes_test_db):
    await store.record_alias_equation(await span(store, "alice", 1), "Tokyo Eye", "TE")
    await store.record_rule_claims(await span(store, "alice", 2), [{"subject": "TE", "verb": "uses", "object": "Atlas"}])
    assert await current(db_pool, "alice", "tokyo eye", "uses") == [("Atlas", "positive")]


@pytest.mark.asyncio
async def test_equation_merges_two_existing_entities_and_their_claims(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [{"subject": "Acme", "verb": "uses", "object": "Atlas"}])
    await store.record_rule_claims(await span(store, "alice", 2), [{"subject": "Acme Corp", "verb": "uses", "object": "Atlas"}])
    eq = await span(store, "alice", 3)
    await store.record_alias_equation(eq, "Acme", "Acme Corp")
    assert await current(db_pool, "alice", "acme", "uses") == [("Atlas", "positive")]
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM claims WHERE owner = 'alice' AND status = 'grounded' AND valid_to IS NULL") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM claim_evidence e JOIN claims c ON c.claim_id = e.claim_id "
            "WHERE c.status = 'grounded' AND c.valid_to IS NULL") == 2


@pytest.mark.asyncio
async def test_abstained_or_assistant_equations_are_ignored(store, clean_hermes_test_db):
    assert not await store.record_alias_equation(await span(store, "alice", 1), "the project", "TP")
    assert not await store.record_alias_equation(await span(store, "alice", 2, role="assistant"), "Acme", "AC")


@pytest.mark.asyncio
async def test_equations_never_cross_owners(store, clean_hermes_test_db):
    await store.record_alias_equation(await span(store, "alice", 1), "Tokyo Eye", "TE")
    assert await store.resolve_entity("bob", "TE") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_claims_aliases.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'record_alias_equation'`

- [ ] **Step 3: Write the implementation** (module functions plus a mixin method in `store_claims.py`)

```python
async def upsert_alias(conn, owner: str, surface_norm: str, entity_id: int, source: str,
                       span_id: Optional[int], event_time: Optional[datetime]) -> None:
    await conn.execute(
        "INSERT INTO entity_alias (owner, surface_norm, entity_id, source, span_id, event_time) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (owner, surface_norm, entity_id) DO UPDATE "
        "SET state = 'live', source = EXCLUDED.source, span_id = EXCLUDED.span_id, "
        "event_time = EXCLUDED.event_time",
        owner, surface_norm, int(entity_id), source, span_id, event_time)


async def reresolve(conn, owner: str, entity_ids: set[int]) -> set[TimelineKey]:
    """Re-resolve live evidence touching these entities from its stored surfaces."""
    keys: set[TimelineKey] = set()
    rows = await conn.fetch(
        "SELECT id, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act, "
        "subject_surface, object_surface FROM claim_evidence WHERE owner = $1 AND state = 'live' "
        "AND (subject_id = ANY($2::bigint[]) OR object_key = ANY($3::text[]))",
        owner, list(entity_ids), [f"e:{i}" for i in entity_ids])
    for r in rows:
        vrec = await verb_row(conn, r["verb"])
        keys.add(timeline_key(owner, int(r["subject_id"]), vrec, r["object_key"]))
        new_sub = await resolve_entity(conn, owner, r["subject_surface"]) or int(r["subject_id"])
        new_obj = r["object_key"]
        if new_obj.startswith("e:"):
            o = await resolve_entity(conn, owner, r["object_surface"])
            if o is not None:
                new_obj = f"e:{o}"
        if (new_sub, new_obj) == (int(r["subject_id"]), r["object_key"]):
            continue
        clash = await conn.fetchval(
            "SELECT id FROM claim_evidence WHERE owner = $1 AND span_id = $2 AND extractor = $3 "
            "AND extractor_version = $4 AND subject_id = $5 AND verb = $6 AND object_key = $7 "
            "AND polarity = $8 AND act = $9 AND state = 'live' AND id <> $10",
            owner, r["span_id"], r["extractor"], r["extractor_version"], new_sub, r["verb"], new_obj,
            r["polarity"], r["act"], r["id"])
        if clash is not None:
            await conn.execute("UPDATE claim_evidence SET state = 'superseded_version' WHERE id = $1", r["id"])
        else:
            await lock_timeline(conn, owner, new_sub, r["verb"])
            holder = await placeholder_claim(conn, owner, new_sub, r["verb"], new_obj, r["polarity"], r["span_id"])
            await conn.execute(
                "UPDATE claim_evidence SET subject_id = $2, object_key = $3, claim_id = $4 WHERE id = $1",
                r["id"], new_sub, new_obj, holder)
        keys.add(timeline_key(owner, new_sub, vrec, new_obj))
    return keys


async def unmerge_unjustified(conn, owner: str) -> set[TimelineKey]:
    rows = await conn.fetch(
        "SELECT e.id, e.merged_into FROM entity e WHERE e.owner = $1 AND e.merged_into IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM entity_alias a WHERE a.owner = e.owner AND a.entity_id = e.merged_into "
        "AND a.surface_norm = e.name_norm AND a.state = 'live' AND a.source IN ('equation', 'manual'))", owner)
    keys: set[TimelineKey] = set()
    for r in rows:
        await conn.execute("UPDATE entity SET merged_into = NULL WHERE owner = $1 AND id = $2", owner, r["id"])
        keys |= await reresolve(conn, owner, {int(r["id"]), int(r["merged_into"])})
    return keys
```

Mixin method:

```python
    async def record_alias_equation(self, span_id: int, canon_surface: str, alias_surface: str) -> bool:
        """Explicit user "X aka Y" (spec §4.1). Attach Y to X; merge when Y names another entity.

        The newer entity merges into the older. The merge is justified by a live
        equation row (merged.name_norm -> survivor); restating an equation moves
        that row to the newer span, so deleting the newer span un-merges.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                info = await span_info(conn, span_id)
                if info.role != "user" or is_abstained(canon_surface) or is_abstained(alias_surface):
                    return False
                canon_id = await ensure_entity(conn, info.owner, canon_surface)
                other = await resolve_entity(conn, info.owner, alias_surface)
                await upsert_alias(conn, info.owner, normalize_name(alias_surface), canon_id, "equation",
                                   span_id, info.ts)
                if other is None or other == canon_id:
                    return True
                types = {r["id"]: r["type"] for r in await conn.fetch(
                    "SELECT id, type FROM entity WHERE owner = $1 AND id = ANY($2::bigint[])",
                    info.owner, [canon_id, other])}
                if "self" in types.values():
                    return True
                survivor, merged = sorted((canon_id, other))
                merged_norm = await conn.fetchval(
                    "SELECT name_norm FROM entity WHERE owner = $1 AND id = $2", info.owner, merged)
                await upsert_alias(conn, info.owner, merged_norm, survivor, "equation", span_id, info.ts)
                await conn.execute("UPDATE entity SET merged_into = $3 WHERE owner = $1 AND id = $2",
                                   info.owner, merged, survivor)
                await rebuild_keys(conn, await reresolve(conn, info.owner, {merged, survivor}))
                return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/test_claims_aliases.py tests/integration/test_claims_timeline.py -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store_claims.py tests/integration/test_claims_aliases.py
git commit -m "feat(claims): alias equations with justified entity merges

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Span deletion API

Spec §4.2 "Deletion"; §4.1 un-merge when the justifying span is deleted.

**Files:**
- Modify: `src/hermes_memory/store_claims.py`, `src/hermes_memory/verify.py:218-240`
- Create: `tests/integration/test_delete_spans.py`

**Interfaces:**
- Produces: `async def delete_spans_on_conn(conn, owner: str, span_ids: Sequence[int]) -> int` and mixin `async def delete_spans(self, owner: str, span_ids: Sequence[int]) -> int` (spans deleted). AGE `Turn` vertices are not touched (unchanged gap; `purge_verify_turns` still handles verify sessions).

- [ ] **Step 1: Confirm what references `conversations`**

Run: `grep -rn "REFERENCES conversations" sql/migrations`
Expected: only `uptakes` (RESTRICT) and the V18 tables (CASCADE). If anything else appears, add its cleanup to `delete_spans_on_conn` before deleting conversations.

- [ ] **Step 2: Write the failing tests**

```python
# tests/integration/test_delete_spans.py
"""Deleting spans rebuilds what they supported (spec §4.2 Deletion)."""
from __future__ import annotations

import pytest

from tests.integration.claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


def lives(obj):
    return [{"subject": "Raul", "verb": "lives in", "object": obj}]


@pytest.mark.asyncio
async def test_deleting_the_newest_value_restores_the_previous_one(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), lives("NYC"))
    newest = await span(store, "alice", 2)
    await store.record_rule_claims(newest, lives("Austin"))
    assert await store.delete_spans("alice", [newest]) == 1
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]


@pytest.mark.asyncio
async def test_deleting_the_only_support_orphans_the_claim(db_pool, store, clean_hermes_test_db):
    s = await span(store, "alice", 1)
    [cid] = await store.record_rule_claims(s, lives("NYC"))
    await store.delete_spans("alice", [s])
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", cid) in ("orphaned", None)


@pytest.mark.asyncio
async def test_uptakes_are_removed_with_their_spans(db_pool, store, clean_hermes_test_db):
    a = await span(store, "alice", 1, role="assistant")
    u = await span(store, "alice", 2)
    await store.write_uptake(a, u, "unknown")
    await store.delete_spans("alice", [u])
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM uptakes") == 0


@pytest.mark.asyncio
async def test_another_owners_span_is_never_deleted(db_pool, store, clean_hermes_test_db):
    bobs = await span(store, "bob", 1)
    assert await store.delete_spans("alice", [bobs]) == 0
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations WHERE id = $1", bobs) == 1


@pytest.mark.asyncio
async def test_deleting_the_equation_span_unmerges(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), [{"subject": "Acme", "verb": "uses", "object": "Atlas"}])
    await store.record_rule_claims(await span(store, "alice", 2), [{"subject": "Acme Corp", "verb": "uses", "object": "Atlas"}])
    eq = await span(store, "alice", 3)
    await store.record_alias_equation(eq, "Acme", "Acme Corp")
    await store.delete_spans("alice", [eq])
    assert await store.resolve_entity("alice", "Acme Corp") != await store.resolve_entity("alice", "Acme")
    assert await current(db_pool, "alice", "acme corp", "uses") == [("Atlas", "positive")]
```

`test_deleting_the_only_support_orphans_the_claim` accepts `None` because a non-legacy row with no evidence left is deleted by the rebuild (spec §4.2 step 5).

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/integration/test_delete_spans.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'delete_spans'`

- [ ] **Step 4: Write the implementation**

```python
async def delete_spans_on_conn(conn, owner: str, span_ids: Sequence[int]) -> int:
    """Delete one owner's spans and rebuild what they supported (spec §4.2 Deletion).

    Must run inside a transaction. Evidence, alias rows and turn_entity rows
    cascade; uptakes are removed first (RESTRICT). AGE Turn vertices are not
    touched here.
    """
    owned = [int(r["id"]) for r in await conn.fetch(
        "SELECT id FROM conversations WHERE agent_identity = $1 AND id = ANY($2::bigint[])",
        owner, [int(i) for i in span_ids])]
    if not owned:
        return 0
    keys: set[TimelineKey] = set()
    for r in await conn.fetch(
            "SELECT DISTINCT subject_id, verb, object_key FROM claim_evidence "
            "WHERE owner = $1 AND span_id = ANY($2::bigint[])", owner, owned):
        keys.add(timeline_key(owner, int(r["subject_id"]), await verb_row(conn, r["verb"]), r["object_key"]))
    await conn.execute("DELETE FROM uptakes WHERE prior_span_id = ANY($1::bigint[]) "
                       "OR next_span_id = ANY($1::bigint[])", owned)
    await conn.execute("UPDATE claims SET span_id = NULL WHERE span_id = ANY($1::bigint[])", owned)
    await conn.execute("DELETE FROM memory_chunk_nodes WHERE source = 'conversation' "
                       "AND chunk_id = ANY($1::text[])", [f"conv_{i}" for i in owned])
    status = await conn.execute("DELETE FROM conversations WHERE agent_identity = $1 AND id = ANY($2::bigint[])",
                                owner, owned)
    keys |= await unmerge_unjustified(conn, owner)
    await rebuild_keys(conn, keys)
    return int(str(status).split()[-1])
```

Mixin:

```python
    async def delete_spans(self, owner: str, span_ids: Sequence[int]) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await delete_spans_on_conn(conn, owner, span_ids)
```

In `src/hermes_memory/verify.py`, replace the block that starts with the comment `# V12 uptakes/claims reference conversations with ON DELETE RESTRICT` and ends with `DELETE FROM conversations WHERE session_id LIKE $1` (the four `conn.execute` calls) with:

```python
        # Synthetic verify spans go through the deletion API so their evidence,
        # uptakes and timelines are cleaned up the same way a user deletion is.
        from .store_claims import delete_spans_on_conn

        for row in await conn.fetch(
            "SELECT agent_identity, array_agg(id) AS ids FROM conversations "
            "WHERE session_id LIKE $1 GROUP BY agent_identity", "verify-c5%",
        ):
            async with conn.transaction():
                await delete_spans_on_conn(conn, row["agent_identity"], list(row["ids"]))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/integration/test_delete_spans.py -q -m integration --override-ini="addopts=" --migrate && pytest -q`
Expected: PASS for both. The unit suite covers any verify unit tests.

- [ ] **Step 6: Commit**

```bash
git add src/hermes_memory/store_claims.py src/hermes_memory/verify.py tests/integration/test_delete_spans.py
git commit -m "feat(claims): span deletion API that rebuilds timelines and un-merges

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Legacy claims data migration

Spec §7.2 steps 2–6.

**Files:**
- Create: `src/hermes_memory/claims_migration.py`, `tests/integration/test_claims_migration.py`
- Modify: `src/hermes_memory/provider.py:221` (`_ainit`)

**Interfaces:**
- Consumes: `Store._graph`, `Store._resolve` (existing alias helpers, kept), Task 4–7 functions
- Produces:
  - `NAME = "claims_evidence_v1"`
  - `async def migrate_legacy_claims(store) -> dict[str, int]` (`{"skipped": 1}` when already applied)
  - `async def legacy_check(conn) -> dict[str, list[dict]]` with keys `violations` and `superseded`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_claims_migration.py
"""Legacy claims keep their meaning through the evidence migration (spec §7.2)."""
from __future__ import annotations

import pytest

from hermes_memory.claims_migration import legacy_check, migrate_legacy_claims
from tests.integration.claims_helpers import current, span

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def legacy(db_pool, span_id, subject, verb, obj, valid, polarity="positive", superseded_by=None):
    """A claim row shaped like V12–V15 wrote it, after V18's provisional pass."""
    status = {"live": "grounded", "retracted": "retracted"}.get(valid, "unconfirmed")
    async with db_pool.acquire() as conn:
        owner = await conn.fetchval("SELECT agent_identity FROM conversations WHERE id = $1", span_id)
        return await conn.fetchval(
            "INSERT INTO claims (owner, span_id, subject, verb, object, polarity, act, subject_canon, "
            "object_canon, status, legacy_valid, superseded_by) VALUES ($1, $2, $3, $4, $5, $6, 'assert', "
            "lower($3), lower($5), $7, $8, $9) RETURNING claim_id",
            owner, span_id, subject, verb, obj, polarity, status, valid, superseded_by)


@pytest.mark.asyncio
async def test_live_claim_stays_current(db_pool, store, clean_hermes_test_db):
    await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "live")
    await migrate_legacy_claims(store)
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "positive")]


@pytest.mark.asyncio
async def test_v15_duplicate_is_a_repeat_saying_not_a_correction(db_pool, store, clean_hermes_test_db):
    keep = await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "live")
    await legacy(db_pool, await span(store, "alice", 2), "Nightingale", "uses", "Atlas", "retracted", superseded_by=keep)
    await migrate_legacy_claims(store)
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "positive")]
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM claim_evidence WHERE act = 'retract'") == 0


@pytest.mark.asyncio
async def test_repaired_claim_stays_retracted(db_pool, store, clean_hermes_test_db):
    new = await legacy(db_pool, await span(store, "alice", 2), "Nightingale", "uses", "Atlas", "live", polarity="negative")
    await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "retracted", superseded_by=new)
    await migrate_legacy_claims(store)
    assert await current(db_pool, "alice", "nightingale", "uses") == [("Atlas", "negative")]


@pytest.mark.asyncio
async def test_retract_never_lands_on_another_owners_span(db_pool, store, clean_hermes_test_db):
    bobs = await legacy(db_pool, await span(store, "bob", 2), "Nightingale", "uses", "Atlas", "live", polarity="negative")
    alices = await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "retracted", superseded_by=bobs)
    await migrate_legacy_claims(store)
    async with db_pool.acquire() as conn:
        owners = {r["owner"] for r in await conn.fetch("SELECT owner FROM claim_evidence WHERE act = 'retract'")}
        assert owners == {"alice"}
        assert await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", alices) != "grounded"


@pytest.mark.asyncio
async def test_abstained_subject_is_orphaned_without_evidence(db_pool, store, clean_hermes_test_db):
    cid = await legacy(db_pool, await span(store, "alice", 1), "They", "uses", "Atlas", "live")
    await migrate_legacy_claims(store)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM claims WHERE claim_id = $1", cid) == "orphaned"


@pytest.mark.asyncio
async def test_aliases_copy_only_for_a_single_owner(db_pool, store, clean_hermes_test_db):
    await span(store, "alice", 1)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO aliases (surface_norm, canon_id, source) VALUES ('te', 'tokyo eye', 'user_span')")
    await migrate_legacy_claims(store)
    assert await store.resolve_entity("alice", "TE") == await store.resolve_entity("alice", "Tokyo Eye")


@pytest.mark.asyncio
async def test_aliases_drop_for_several_owners(db_pool, store, clean_hermes_test_db):
    await span(store, "alice", 1)
    await span(store, "bob", 2)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO aliases (surface_norm, canon_id, source) VALUES ('te', 'tokyo eye', 'user_span')")
    result = await migrate_legacy_claims(store)
    assert result["aliases_dropped"] == 1
    assert await store.resolve_entity("alice", "TE") is None


@pytest.mark.asyncio
async def test_second_run_is_skipped_and_check_is_clean(db_pool, store, clean_hermes_test_db):
    await legacy(db_pool, await span(store, "alice", 1), "Nightingale", "uses", "Atlas", "live")
    await migrate_legacy_claims(store)
    assert await migrate_legacy_claims(store) == {"skipped": 1}
    async with db_pool.acquire() as conn:
        assert (await legacy_check(conn))["violations"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_claims_migration.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_memory.claims_migration'`

- [ ] **Step 3: Write the implementation**

```python
# src/hermes_memory/claims_migration.py
"""One-shot legacy claims → evidence migration (graph layer spec §7.2 steps 2–6).

Runs once per database (data_migrations 'claims_evidence_v1') under an
advisory lock, in one transaction, after V18. Pre-migration truth is
claims.legacy_valid; legacy_check compares it per value afterwards.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import timedelta

from .entities import is_abstained
from .store_claims import (
    ensure_entity,
    object_key_for,
    rebuild_keys,
    timeline_key,
    upsert_alias,
    verb_row,
)

logger = logging.getLogger("hermes_memory.claims_migration")
NAME = "claims_evidence_v1"
LEGACY_VERSION = "legacy"

_LEGACY_ROWS = """
SELECT c.claim_id, c.span_id, c.subject, c.verb, c.object, c.polarity, c.legacy_valid, c.superseded_by,
       c.subject_canon, c.object_canon, cv.agent_identity AS owner, cv.ts, cv.role
  FROM claims c JOIN conversations cv ON cv.id = c.span_id
 WHERE c.legacy_valid IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.owner = c.owner AND e.claim_id = c.claim_id)
 ORDER BY c.claim_id
"""
_EVIDENCE = """
INSERT INTO claim_evidence (owner, claim_id, span_id, speaker, act, polarity, subject_surface,
    object_surface, subject_id, verb, object_key, extractor, extractor_version, event_time)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'rules', 'legacy', $12)
ON CONFLICT (owner, span_id, extractor, extractor_version, subject_id, verb, object_key, polarity, act)
WHERE state = 'live' DO NOTHING
"""


async def _copy_aliases(conn, owner: str) -> int:
    from .store import Store

    rows = await conn.fetch("SELECT surface_norm, canon_id, created_at, alias_id FROM aliases WHERE valid = 'live'")
    amap, meta = Store._graph(rows)
    copied = 0
    for r in rows:
        canon, _ = Store._resolve(r["canon_id"], amap, meta)
        if is_abstained(canon) or is_abstained(r["surface_norm"]) or canon == r["surface_norm"]:
            continue
        await upsert_alias(conn, owner, r["surface_norm"], await ensure_entity(conn, owner, canon),
                           "migration", None, None)
        copied += 1
    return copied


async def _retract_site(conn, r, by_id) -> tuple[int, object]:
    sup = by_id.get(r["superseded_by"]) if r["superseded_by"] is not None else None
    if sup is not None and sup["owner"] == r["owner"]:
        return int(sup["span_id"]), sup["ts"]
    rep = await conn.fetchrow(
        "SELECT u.next_span_id, cv.ts FROM uptakes u JOIN conversations cv ON cv.id = u.next_span_id "
        "WHERE u.prior_span_id = $1 AND u.value = 'repaired' AND cv.agent_identity = $2 "
        "ORDER BY u.next_span_id LIMIT 1", r["span_id"], r["owner"])
    if rep is not None:
        return int(rep["next_span_id"]), rep["ts"]
    return int(r["span_id"]), r["ts"] + timedelta(microseconds=1)


async def migrate_legacy_claims(store) -> dict[str, int]:
    async with store.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"hermes_memory:data:{NAME}")
            if await conn.fetchval("SELECT 1 FROM data_migrations WHERE name = $1", NAME):
                return {"skipped": 1}
            counts: Counter = Counter()
            owners = [r["agent_identity"] for r in await conn.fetch(
                "SELECT DISTINCT agent_identity FROM conversations")]
            if len(owners) == 1:
                counts["aliases_copied"] = await _copy_aliases(conn, owners[0])
            else:
                counts["aliases_dropped"] = int(await conn.fetchval(
                    "SELECT count(*) FROM aliases WHERE valid = 'live'"))
            rows = await conn.fetch(_LEGACY_ROWS)
            by_id = {int(r["claim_id"]): r for r in rows}
            keys = set()
            for r in rows:
                vrec = await verb_row(conn, r["verb"])
                object_key = None if is_abstained(r["object"]) else await object_key_for(conn, r["owner"], r["object"])
                if vrec is None or is_abstained(r["subject"]) or object_key is None:
                    await conn.execute("UPDATE claims SET status = 'orphaned' WHERE claim_id = $1", r["claim_id"])
                    counts["orphaned"] += 1
                    continue
                subject_id = await ensure_entity(conn, r["owner"], r["subject"])
                oid = int(object_key[2:]) if object_key.startswith("e:") else None
                canon = (await conn.fetchval("SELECT name_norm FROM entity WHERE owner = $1 AND id = $2",
                                             r["owner"], oid)) if oid is not None else object_key[2:]
                await conn.execute(
                    "UPDATE claims SET status = 'orphaned', subject_id = $2, object_id = $3, object_canon = $4, "
                    "verb = $5 WHERE claim_id = $1", r["claim_id"], subject_id, oid, canon, vrec["verb"])
                speaker = "assistant" if r["role"] == "assistant" else "user"
                await conn.execute(_EVIDENCE, r["owner"], r["claim_id"], r["span_id"], speaker, "assert",
                                   r["polarity"], r["subject"], r["object"], subject_id, vrec["verb"],
                                   object_key, r["ts"])
                sup = by_id.get(r["superseded_by"]) if r["superseded_by"] is not None else None
                same_value = sup is not None and (sup["subject_canon"], sup["verb"], sup["object_canon"],
                                                  sup["polarity"]) == (r["subject_canon"], r["verb"],
                                                                       r["object_canon"], r["polarity"])
                if r["legacy_valid"] == "retracted" and same_value:
                    counts["deduplicated"] += 1
                elif r["legacy_valid"] == "retracted":
                    site, when = await _retract_site(conn, r, by_id)
                    await conn.execute(_EVIDENCE, r["owner"], r["claim_id"], site, "user", "retract",
                                       r["polarity"], r["subject"], r["object"], subject_id, vrec["verb"],
                                       object_key, when)
                    counts["retracted"] += 1
                else:
                    counts[r["legacy_valid"]] += 1
                keys.add(timeline_key(r["owner"], subject_id, vrec, object_key))
            await rebuild_keys(conn, keys)
            await conn.execute("INSERT INTO data_migrations (name, detail) VALUES ($1, $2::jsonb)",
                               NAME, json.dumps(dict(counts)))
            return dict(counts)


_CHECK = """
WITH v AS (
  SELECT c.owner, c.subject_id, c.verb,
         CASE WHEN c.object_id IS NOT NULL THEN 'e:' || c.object_id::text ELSE 'l:' || c.object_canon END AS object_key,
         c.polarity,
         bool_or(c.legacy_valid = 'live') AS any_live,
         bool_and(c.legacy_valid = 'retracted') AS all_retracted
    FROM claims c
   WHERE c.legacy_valid IS NOT NULL AND c.subject_id IS NOT NULL
   GROUP BY 1, 2, 3, 4, 5)
SELECT v.*,
       EXISTS (SELECT 1 FROM claims g WHERE g.owner = v.owner AND g.subject_id = v.subject_id
                 AND g.verb = v.verb AND g.polarity = v.polarity AND g.status = 'grounded' AND g.valid_to IS NULL
                 AND (CASE WHEN g.object_id IS NOT NULL THEN 'e:' || g.object_id::text ELSE 'l:' || g.object_canon END)
                     = v.object_key) AS is_current,
       EXISTS (SELECT 1 FROM claims g WHERE g.owner = v.owner AND g.subject_id = v.subject_id
                 AND g.verb = v.verb AND g.status = 'grounded' AND g.valid_to IS NULL) AS key_has_current
  FROM v
"""


async def legacy_check(conn) -> dict[str, list[dict]]:
    """§7.2 step 6, per value. A live value replaced by a newer statement is listed, not a violation."""
    out: dict[str, list[dict]] = {"violations": [], "superseded": []}
    for r in await conn.fetch(_CHECK):
        row = {k: r[k] for k in ("owner", "subject_id", "verb", "object_key", "polarity")}
        if r["any_live"] and not r["is_current"]:
            (out["superseded"] if r["key_has_current"] else out["violations"]).append(row)
        elif r["all_retracted"] and r["is_current"]:
            out["violations"].append(row)
    return out
```

In `src/hermes_memory/provider.py` `_ainit`, directly after `await self.store.require_schema_head()`:

```python
        try:
            from .claims_migration import migrate_legacy_claims

            migrated = await migrate_legacy_claims(self.store)
            if not migrated.get("skipped"):
                logger.info("claims data migration applied: %s", migrated)
        except Exception:
            logger.warning("claims data migration failed; legacy claims stay provisional", exc_info=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/test_claims_migration.py -q -m integration --override-ini="addopts=" --migrate && pytest -q`
Expected: PASS for both

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/claims_migration.py src/hermes_memory/provider.py tests/integration/test_claims_migration.py
git commit -m "feat(claims): migrate legacy claims to evidence without changing their meaning

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Reception writes evidence

Spec §5.3; §4.2 "Legacy columns" (no direct `valid` writers remain).

**Files:**
- Modify: `src/hermes_memory/reception.py:14-34` (`ReceptionStore`), `src/hermes_memory/provider.py:537-605` (`_reception_stage`), `src/hermes_memory/store.py` (remove `insert_alias`, `alias_map`, `alias_graph`, `_canon`, `insert_claims`, `retract_on_repair`; drop the `UPDATE claims SET uptake` statement in `write_uptake`; rewrite `span_overlays`)
- Test: `tests/test_reception_stage.py`, `tests/integration/test_claims_repair.py` (overlay test)

**Interfaces:**
- Consumes: `record_alias_equation`, `record_rule_claims`, `record_repair` (Tasks 5–7)
- Produces: `ReceptionStore` with `record_alias_equation`, `record_rule_claims`, `previous_turn`, `write_uptake`, `record_repair`, `stamp_claims_status`, `fetch_pending_reception`. `span_overlays(ids) -> {id: {"uptake", "live_claims", "retracted"}}`, same shape as before.

- [ ] **Step 1: Update the unit test fake and expectations**

In `tests/test_reception_stage.py`, replace `FakeStore.insert_alias`, `insert_claims` and `retract_on_repair` with:

```python
    async def record_alias_equation(self, span_id: int, canon: str, surface: str) -> bool:
        self.aliases.append((surface, canon, "user_span"))
        return True

    async def record_rule_claims(self, span_id: int, claims: list) -> list:
        self.claim_inserts.append((span_id, claims))
        return [next(self._ids) for _ in claims]

    async def record_repair(self, prior_id: int, next_id: int, *, deictic: bool, new_claims=()) -> int:
        self.retractions.append((prior_id, next_id, deictic, list(new_claims)))
        return 1
```

Then update every assertion on `store.retractions` from the old tuple shape `(prior_id, next_id, text, claims)` to `(prior_id, next_id, deictic, claims)`. Run `pytest tests/test_reception_stage.py -q` and fix each assertion the failures name. Add:

```python
async def test_deictic_repair_without_new_claims_is_flagged_deictic():
    store = FakeStore(prior={"id": 10, "content": "Nightingale uses Atlas."})
    await make_provider()._reception_stage(store, 11, "s", "user", "No, not that.")
    assert store.retractions == [(10, 11, True, [])]
```

- [ ] **Step 2: Run the unit tests to verify they fail**

Run: `pytest tests/test_reception_stage.py -q`
Expected: FAIL (the provider still calls `insert_claims` / `retract_on_repair`)

- [ ] **Step 3: Rewrite the protocol and the stage**

`src/hermes_memory/reception.py`, replace the `ReceptionStore` body:

```python
class ReceptionStore(Protocol):
    """Store seam for the reception stage: evidence writers, uptake, catch-up."""

    async def record_alias_equation(self, span_id: int, canon_surface: str, alias_surface: str) -> bool: ...
    async def record_rule_claims(self, span_id: int, claims: Sequence[dict]) -> list[int]: ...
    async def previous_turn(
        self, session_id: str, conv_id: int, *, role: str = "assistant"
    ) -> dict | None: ...
    async def write_uptake(self, prior_span_id: int, next_span_id: int, value: str) -> None: ...
    async def record_repair(self, prior_span_id: int, repair_span_id: int, *, deictic: bool,
                            new_claims: Sequence[dict] = ()) -> int: ...
    async def stamp_claims_status(self, conv_id: int, status: str) -> None: ...
    async def fetch_pending_reception(self, limit: int) -> list[dict]: ...
```

`src/hermes_memory/provider.py` `_reception_stage`: update the import to add `is_deictic_repair`, and replace everything from `wrote = False` to the final `return` with:

```python
        wrote = False
        for surface, canon in extract_alias_equations(content):
            try:
                if await store.record_alias_equation(conv_id, canon, surface):
                    wrote = True
            except Exception:
                logger.warning("alias write failed", exc_info=True)
        prior: dict | None = None
        try:
            prior = await store.previous_turn(session_id, conv_id, role="assistant")
        except Exception:
            logger.warning("previous turn lookup failed", exc_info=True)
        verdict = (
            classify_repair(str(prior.get("content") or ""), content)
            if prior is not None
            else "unknown"
        )
        if prior is not None:
            try:
                await store.write_uptake(int(prior["id"]), conv_id, verdict)
                wrote = True
            except Exception:
                logger.warning("uptake write failed", exc_info=True)
        parsed: list[dict] = []
        try:
            parsed = extract_assertions(content)
            if parsed and await store.record_rule_claims(conv_id, parsed):
                wrote = True
        except Exception:
            logger.warning("claim write failed", exc_info=True)
        if prior is not None and verdict == "repaired":
            try:
                deictic = not parsed and is_deictic_repair(content)
                if await store.record_repair(int(prior["id"]), conv_id, deictic=deictic, new_claims=parsed):
                    wrote = True
            except Exception:
                logger.warning("retract failed", exc_info=True)
        return await _stamp("ready" if wrote else "none")
```

Also update the docstring's last sentence to: `Assistant spans yield no claims here; model speech is evidence only through extraction jobs (Phase 2).`

- [ ] **Step 4: Remove the legacy writers and rewrite `span_overlays` in `store.py`**

Delete `insert_alias`, `alias_map`, `alias_graph`, `_canon`, `insert_claims` and `retract_on_repair`. Keep `_graph`, `_elect` and `_resolve` (used by `tests/test_dedupe.py` and `claims_migration._copy_aliases`). In `write_uptake`, delete the final `if row is not None: await conn.execute("UPDATE claims SET uptake = …")` block and the `row = await conn.fetchrow(...)` that feeds it.

Replace the two `claims` queries inside `span_overlays` with:

```python
            for r in await conn.fetch(
                """SELECT e.span_id,
                          count(DISTINCT c.claim_id) FILTER (
                              WHERE c.status = 'grounded' AND c.valid_to IS NULL) AS live
                     FROM claim_evidence e
                     JOIN claims c ON c.owner = e.owner AND c.claim_id = e.claim_id
                    WHERE e.span_id = ANY($1::bigint[]) AND e.state = 'live' AND e.act = 'assert'
                    GROUP BY e.span_id""",
                wanted,
            ):
                out[int(r["span_id"])]["live_claims"] = int(r["live"])
            for r in await conn.fetch(
                """SELECT DISTINCT e.span_id, c.claim_id, c.subject, c.verb, c.object
                     FROM claim_evidence e
                     JOIN claims c ON c.owner = e.owner AND c.claim_id = e.claim_id
                    WHERE e.span_id = ANY($1::bigint[]) AND c.status = 'retracted'
                    ORDER BY e.span_id, c.claim_id
                    LIMIT 12""",
                wanted,
            ):
```

(the loop body that appends `subject`, `verb`, `object` stays unchanged).

Run: `grep -rn "insert_claims\|retract_on_repair\|insert_alias\|alias_map\|alias_graph\|_canon(" src scripts tests`
Expected: no matches outside `tests/test_dedupe.py` comments.

- [ ] **Step 5: Add an overlay integration test** (append to `tests/integration/test_claims_repair.py`)

```python
@pytest.mark.asyncio
async def test_span_overlays_read_status(store, clean_hermes_test_db):
    u = await span(store, "alice", 1)
    await store.record_rule_claims(u, [ATLAS])
    a = await span(store, "alice", 2, role="assistant")
    await store.record_rule_claims(a, [{"subject": "Nightingale", "verb": "uses", "object": "Borealis"}])
    fix = await span(store, "alice", 3)
    await store.record_repair(a, fix, deictic=True)
    overlays = await store.span_overlays([u, a])
    assert overlays[u]["live_claims"] == 1
    assert overlays[a]["retracted"] == [{"subject": "Nightingale", "verb": "uses", "object": "Borealis"}]
```

- [ ] **Step 6: Run everything to verify**

Run: `pytest -q && pytest tests/integration -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS (unit and integration)

- [ ] **Step 7: Commit**

```bash
git add src/hermes_memory/reception.py src/hermes_memory/provider.py src/hermes_memory/store.py tests/test_reception_stage.py tests/integration/test_claims_repair.py
git commit -m "feat(reception): write claims as evidence; remove direct claim and alias writers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Durable jobs, with capture moved onto them

Spec §5.1; §7.3 (additive: `write_outbox` stays until Phase 4).

**Files:**
- Create: `sql/migrations/V19__durable_job.sql`, `src/hermes_memory/store_jobs.py`, `tests/integration/test_durable_job.py`
- Modify: `src/hermes_memory/store.py` (remove the `outbox_*` methods, lines 726–815, and mix in `StoreJobsMixin`; keep `OUTBOX_MAX_ATTEMPTS`), `src/hermes_memory/schema_guard.py:84`, `tests/conftest.py`, `tests/integration/test_outbox.py`, `scripts/eval_memory.py:275`, `tests/test_schema_guard.py`

**Interfaces:**
- Produces (`StoreJobsMixin`):
  - `job_add(owner, kind, op_id, payload, *, claimed_by=None, lease_s=600.0, session_id=None, event_time=None, priority=0) -> tuple[int, int] | None`
  - `job_claim(kind, worker, limit, *, lease_s, min_age_s=0.0, max_attempts=5, session_ordered=False) -> list[tuple[int, int, dict]]`
  - `job_heartbeat(job_id, fence, lease_s) -> bool`
  - `job_done(job_id, fence, *, delete=False) -> bool`
  - `job_release(job_id, fence, error=None) -> bool`
  - `job_retry(job_id, fence, error) -> str` (`retryable` | `failed` | `stale`)
  - `job_release_worker(worker) -> int`
  - `job_counts() -> dict[str, dict[str, int]]`
  - `async def job_fence_check(conn, job_id, fence) -> bool` (module function; locks the row; Phase 2 publish uses it)
  - The `outbox_*` facade keeps the Phase 0 signatures and semantics, so `provider.py` does not change.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_durable_job.py
"""durable_job lifecycle (spec §5.1)."""
from __future__ import annotations

import asyncio

import pytest

from hermes_memory.store_jobs import job_fence_check

pytestmark = [pytest.mark.store, pytest.mark.integration]


async def _expire(db_pool, job_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET lease_expires_at = now() - interval '1 second' WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_two_claimers_never_get_the_same_job(store, clean_hermes_test_db):
    for i in range(8):
        await store.job_add("alice", "extract_rules", f"op-{i}", {"span_id": i})
    a, b = await asyncio.gather(store.job_claim("extract_rules", "w1", 5, lease_s=60),
                                store.job_claim("extract_rules", "w2", 5, lease_s=60))
    ids_a, ids_b = {j for j, _, _ in a}, {j for j, _, _ in b}
    assert ids_a.isdisjoint(ids_b) and len(ids_a | ids_b) == 8


@pytest.mark.asyncio
async def test_session_order_blocks_later_jobs_of_the_same_kind_only(store, clean_hermes_test_db):
    await store.job_add("alice", "extract_rules", "op-1", {}, session_id="s", event_time=None)
    async with store.pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET event_time = now() - interval '2 minutes' WHERE op_id = 'op-1'")
    await store.job_add("alice", "extract_rules", "op-2", {}, session_id="s")
    await store.job_add("alice", "extract_model", "op-3", {}, session_id="s")
    async with store.pool.acquire() as conn:
        await conn.execute("UPDATE durable_job SET event_time = now() WHERE op_id IN ('op-2', 'op-3')")
    [(first, _, _)] = await store.job_claim("extract_rules", "w", 10, lease_s=60, session_ordered=True)
    assert await store.job_claim("extract_rules", "w", 10, lease_s=60, session_ordered=True) == []
    assert len(await store.job_claim("extract_model", "w", 10, lease_s=60, session_ordered=True)) == 1


@pytest.mark.asyncio
async def test_stale_worker_cannot_publish_after_reclaim(store, db_pool, clean_hermes_test_db):
    await store.job_add("alice", "extract_rules", "op-x", {})
    [(jid, fence_a, _)] = await store.job_claim("extract_rules", "w1", 1, lease_s=60)
    await _expire(db_pool, jid)
    [(_, fence_b, _)] = await store.job_claim("extract_rules", "w2", 1, lease_s=60)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            assert await job_fence_check(conn, jid, fence_a) is False
            assert await job_fence_check(conn, jid, fence_b) is True
    assert await store.job_done(jid, fence_a) is False
    assert await store.job_heartbeat(jid, fence_a, 60) is False
    assert await store.job_done(jid, fence_b) is True


@pytest.mark.asyncio
async def test_retry_backs_off_then_fails_after_the_limit(store, db_pool, clean_hermes_test_db):
    await store.job_add("alice", "extract_model", "op-r", {})
    outcomes = []
    for _ in range(5):
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE durable_job SET next_attempt_at = now() - interval '1 second' WHERE op_id = 'op-r'")
        [(jid, fence, _)] = await store.job_claim("extract_model", "w", 1, lease_s=60)
        outcomes.append(await store.job_retry(jid, fence, "boom"))
    assert outcomes == ["retryable"] * 4 + ["failed"]
    assert (await store.job_counts())["extract_model"]["failed"] == 1


@pytest.mark.asyncio
async def test_v19_copies_pending_outbox_rows_and_is_idempotent(db_pool, clean_hermes_test_db):
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "sql" / "migrations" / "V19__durable_job.sql").read_text()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM write_outbox")
        await conn.execute("INSERT INTO write_outbox (agent_identity, item, op_id, attempts) "
                           "VALUES ('alice', '{\"type\": \"turn\"}', 'op-legacy', 1), "
                           "('alice', '{\"type\": \"turn\"}', 'op-poison', 5)")
        await conn.execute(sql)
        await conn.execute(sql)
        rows = {r["op_id"]: r["status"] for r in await conn.fetch(
            "SELECT op_id, status FROM durable_job WHERE kind = 'capture' AND owner = 'alice'")}
        assert rows == {"op-legacy": "pending", "op-poison": "failed"}
        assert await conn.fetchval("SELECT count(*) FROM write_outbox") == 0
```

Port `tests/integration/test_outbox.py` to the new table: change `_reset` to `DELETE FROM durable_job WHERE kind = 'capture'`; in `test_parked_rows_are_not_claimed_and_are_counted` replace the `UPDATE write_outbox …` statement with
`UPDATE durable_job SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL, attempts = $2 WHERE id = $1`;
in `test_a_stale_worker_cannot_settle_a_row_someone_else_reclaimed` replace the `UPDATE write_outbox …` statement with
`UPDATE durable_job SET lease_expires_at = now() - interval '1 second', created_at = now() - interval '1 hour' WHERE id = $1`.
Add to `tests/test_schema_guard.py`: `assert "V19" in CONSUMER_HEAD_VERSIONS` inside `test_head_versions_include_claims_evidence`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_durable_job.py tests/integration/test_outbox.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL (`relation "durable_job" does not exist`)

- [ ] **Step 3: Write the migration**

```sql
-- V19__durable_job.sql — one lease-and-fence job lifecycle for capture and
-- extraction (graph layer spec v0.5.1 §5.1). Capture moves off write_outbox:
-- pending rows are copied here; write_outbox stays (empty) until Phase 4 so
-- an older release can still start. Idempotent: safe to re-run.

SET search_path = public;

CREATE TABLE IF NOT EXISTS durable_job (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('capture', 'extract_rules', 'extract_model')),
    op_id TEXT NOT NULL,
    session_id TEXT,
    event_time TIMESTAMPTZ,
    priority INT NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'retryable', 'done', 'failed')),
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    fence BIGINT NOT NULL DEFAULT 0,
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, kind, op_id)
);
CREATE INDEX IF NOT EXISTS idx_durable_job_session_order ON durable_job (owner, session_id, kind, event_time)
    WHERE status IN ('pending', 'leased', 'retryable');
CREATE INDEX IF NOT EXISTS idx_durable_job_claimable ON durable_job (kind, priority, event_time, id)
    WHERE status IN ('pending', 'leased', 'retryable');

INSERT INTO durable_job (owner, kind, op_id, payload, status, attempts, last_error, created_at, event_time)
SELECT agent_identity, 'capture', coalesce(op_id, 'outbox:' || id::text), item,
       CASE WHEN attempts >= 5 THEN 'failed' ELSE 'pending' END, attempts, last_error, created_at, created_at
  FROM write_outbox
ON CONFLICT (owner, kind, op_id) DO NOTHING;
DELETE FROM write_outbox wo
 WHERE EXISTS (SELECT 1 FROM durable_job j WHERE j.kind = 'capture' AND j.owner = wo.agent_identity
                 AND j.op_id = coalesce(wo.op_id, 'outbox:' || wo.id::text));
```

- [ ] **Step 4: Write `store_jobs.py`**

```python
# src/hermes_memory/store_jobs.py
"""Durable jobs: one lease-and-fence lifecycle for capture and extraction (spec §5.1).

The fence increments on every claim; every settle, heartbeat and publish
requires the current fence, so a worker whose lease expired cannot act on a
row someone else now holds. The outbox_* methods are the capture facade with
the Phase 0 signatures.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

JOB_MAX_ATTEMPTS = 5
CAPTURE_LEASE_S = 600.0
_BACKOFF_SQL = "(ARRAY[5, 30, 120, 600])[LEAST(attempts, 4)]"
_LIVE = "('pending', 'leased', 'retryable')"


def _payload(raw) -> dict:
    return dict(json.loads(raw) if isinstance(raw, str) else raw)


def _count(status: str) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def job_fence_check(conn, job_id: int, fence: int) -> bool:
    """Lock the job row and confirm the caller still holds it. Use inside the publish transaction."""
    row = await conn.fetchval(
        "SELECT 1 FROM durable_job WHERE id = $1 AND fence = $2 AND status = 'leased' FOR UPDATE",
        int(job_id), int(fence))
    return row is not None


class StoreJobsMixin:
    pool: Any

    async def job_add(self, owner: str, kind: str, op_id: str, payload: Dict[str, Any], *,
                      claimed_by: Optional[str] = None, lease_s: float = CAPTURE_LEASE_S,
                      session_id: Optional[str] = None, event_time=None, priority: int = 0):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO durable_job (owner, kind, op_id, payload, session_id, event_time, priority, "
                "status, claimed_by, lease_expires_at, fence) VALUES ($1, $2, $3, $4::jsonb, $5, "
                "coalesce($6, now()), $7, CASE WHEN $8::text IS NULL THEN 'pending' ELSE 'leased' END, $8, "
                "CASE WHEN $8::text IS NULL THEN NULL ELSE now() + make_interval(secs => $9) END, "
                "CASE WHEN $8::text IS NULL THEN 0 ELSE 1 END) "
                "ON CONFLICT (owner, kind, op_id) DO NOTHING RETURNING id, fence",
                owner, kind, op_id, json.dumps(payload), session_id, event_time, int(priority),
                claimed_by, float(lease_s))
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id, fence FROM durable_job WHERE owner = $1 AND kind = $2 AND op_id = $3 "
                    "AND status <> 'done'", owner, kind, op_id)
        return (int(row["id"]), int(row["fence"])) if row is not None else None

    async def job_claim(self, kind: str, worker: str, limit: int, *, lease_s: float,
                        min_age_s: float = 0.0, max_attempts: int = JOB_MAX_ATTEMPTS,
                        session_ordered: bool = False) -> list[tuple[int, int, dict]]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE durable_job SET status = 'failed', claimed_by = NULL, lease_expires_at = NULL "
                    "WHERE kind = $1 AND attempts >= $2 AND (status IN ('pending', 'retryable') "
                    "OR (status = 'leased' AND lease_expires_at < now()))", kind, int(max_attempts))
                rows = await conn.fetch(
                    f"""
                    UPDATE durable_job j
                       SET status = 'leased', claimed_by = $2,
                           lease_expires_at = now() + make_interval(secs => $3),
                           fence = j.fence + 1, attempts = j.attempts + 1
                     WHERE j.id IN (
                           SELECT c.id FROM durable_job c
                            WHERE c.kind = $1 AND c.attempts < $5
                              AND (c.status = 'pending'
                                   OR (c.status = 'retryable' AND c.next_attempt_at <= now())
                                   OR (c.status = 'leased' AND c.lease_expires_at < now()))
                              AND c.created_at < now() - make_interval(secs => $4)
                              AND (NOT $6::boolean OR NOT EXISTS (
                                    SELECT 1 FROM durable_job e
                                     WHERE e.owner = c.owner AND e.kind = c.kind
                                       AND e.session_id = c.session_id AND e.status IN {_LIVE}
                                       AND (e.event_time, e.id) < (c.event_time, c.id)))
                            ORDER BY c.priority, c.event_time NULLS LAST, c.id
                            LIMIT $7
                            FOR UPDATE SKIP LOCKED)
                    RETURNING j.id, j.fence, j.payload
                    """,
                    kind, worker, float(lease_s), float(min_age_s), int(max_attempts),
                    bool(session_ordered), int(limit))
        return [(int(r["id"]), int(r["fence"]), _payload(r["payload"])) for r in rows]

    async def job_heartbeat(self, job_id: int, fence: int, lease_s: float) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET lease_expires_at = now() + make_interval(secs => $3) "
                "WHERE id = $1 AND fence = $2 AND status = 'leased'", int(job_id), int(fence), float(lease_s))
        return _count(status) == 1

    async def job_done(self, job_id: int, fence: int, *, delete: bool = False) -> bool:
        async with self.pool.acquire() as conn:
            if delete:
                status = await conn.execute("DELETE FROM durable_job WHERE id = $1 AND fence = $2",
                                            int(job_id), int(fence))
            else:
                status = await conn.execute(
                    "UPDATE durable_job SET status = 'done', claimed_by = NULL, lease_expires_at = NULL "
                    "WHERE id = $1 AND fence = $2 AND status = 'leased'", int(job_id), int(fence))
        return _count(status) == 1

    async def job_release(self, job_id: int, fence: int, error: Optional[str] = None) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL, "
                "last_error = coalesce($3, last_error) WHERE id = $1 AND fence = $2 AND status = 'leased'",
                int(job_id), int(fence), error)
        return _count(status) == 1

    async def job_retry(self, job_id: int, fence: int, error: str) -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                f"UPDATE durable_job SET status = CASE WHEN attempts >= $4 THEN 'failed' ELSE 'retryable' END, "
                f"next_attempt_at = now() + make_interval(secs => {_BACKOFF_SQL}), claimed_by = NULL, "
                f"lease_expires_at = NULL, last_error = $3 "
                f"WHERE id = $1 AND fence = $2 AND status = 'leased' RETURNING status",
                int(job_id), int(fence), error, JOB_MAX_ATTEMPTS)
        return row or "stale"

    async def job_release_worker(self, worker: str) -> int:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE durable_job SET status = 'pending', claimed_by = NULL, lease_expires_at = NULL "
                "WHERE claimed_by = $1 AND status = 'leased'", worker)
        return _count(status)

    async def job_counts(self) -> dict[str, dict[str, int]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT kind, status, count(*)::int AS n FROM durable_job GROUP BY 1, 2")
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
        return out

    # -- capture facade (Phase 0 signatures) -------------------------------------

    async def outbox_add(self, agent_identity: str, item: Dict[str, Any], claimed_by: str, op_id: str):
        return await self.job_add(agent_identity, "capture", op_id, item, claimed_by=claimed_by,
                                  lease_s=CAPTURE_LEASE_S)

    async def outbox_counts(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT count(*) FILTER (WHERE status IN {_LIVE} AND attempts < $1)::int AS pending, "
                f"count(*) FILTER (WHERE status = 'failed' OR (status IN {_LIVE} AND attempts >= $1))::int "
                f"AS parked FROM durable_job WHERE kind = 'capture'", JOB_MAX_ATTEMPTS)
        return {"pending": int(row["pending"]), "parked": int(row["parked"])}

    async def outbox_done(self, outbox_id: int, fence: int) -> bool:
        return await self.job_done(outbox_id, fence, delete=True)

    async def outbox_unclaim(self, outbox_id: int, fence: int, error: Optional[str] = None) -> bool:
        return await self.job_release(outbox_id, fence, error)

    async def outbox_release(self, claimed_by: str) -> int:
        return await self.job_release_worker(claimed_by)

    async def outbox_claim_stale(self, claimed_by: str, limit: int, *, min_age_s: float = 30.0,
                                 stale_claim_s: float = CAPTURE_LEASE_S,
                                 max_attempts: int = JOB_MAX_ATTEMPTS) -> list[tuple[int, int, dict]]:
        return await self.job_claim("capture", claimed_by, limit, lease_s=stale_claim_s,
                                    min_age_s=min_age_s, max_attempts=max_attempts)
```

In `store.py`: delete the `outbox_*` methods (lines 726–815 before this task's edits), import `from .store_jobs import StoreJobsMixin`, and set `class Store(StoreExpandMixin, StoreMergeMixin, StoreConceptsMixin, StoreClaimsMixin, StoreJobsMixin):`. Keep `OUTBOX_MAX_ATTEMPTS = 5` (tests import it).

Head versions: `CONSUMER_HEAD_VERSIONS = ("V1", "V9", "V10", "V11", "V16", "V17", "V18", "V19")`.
Fixture: add `durable_job` to both `TRUNCATE` lists in `tests/conftest.py`.
`scripts/eval_memory.py:275`: replace the leftover query with
`leftover = await conn.fetchval("select count(*) from durable_job where kind = 'capture' and owner = $1", identity)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest -q && pytest tests/integration -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS. `tests/test_write_outbox.py` (fake store) and `tests/integration/test_outbox.py` (real table) both pass unchanged in meaning.

- [ ] **Step 6: Commit**

```bash
git add sql/migrations/V19__durable_job.sql src/hermes_memory/store_jobs.py src/hermes_memory/store.py src/hermes_memory/schema_guard.py tests/conftest.py tests/integration/test_durable_job.py tests/integration/test_outbox.py tests/test_schema_guard.py scripts/eval_memory.py
git commit -m "feat(jobs): durable_job lifecycle; capture moved off write_outbox

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Observability and drift repair

Spec §9 "Observability" (Phase 1 part), §4.2 "Deletion" (verify repairs stale status).

**Files:**
- Modify: `src/hermes_memory/store_claims.py`, `src/hermes_memory/graph_runtime.py:173-185`, `src/hermes_memory/verify.py:257-269`
- Test: `tests/test_graph_api.py`, `tests/integration/test_delete_spans.py`

**Interfaces:**
- Produces:
  - `async def claims_counts_on_conn(conn) -> dict[str, int]` (status → count; NULL counts as `unmigrated`)
  - `async def drift_keys(conn, limit: int = 500) -> set[TimelineKey]`
  - `async def repair_drift_on_conn(conn) -> int` (keys rebuilt)
  - mixin `claims_counts()`, `repair_drift()`
  - health payload gains `claims` and `jobs`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_delete_spans.py`:

```python
@pytest.mark.asyncio
async def test_raw_sql_deletion_is_detected_and_repaired(db_pool, store, clean_hermes_test_db):
    await store.record_rule_claims(await span(store, "alice", 1), lives("NYC"))
    newest = await span(store, "alice", 2)
    await store.record_rule_claims(newest, lives("Austin"))
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE id = $1", newest)  # bypasses the API
    assert await store.repair_drift() >= 1
    assert await current(db_pool, "alice", "raul", "lives_in") == [("NYC", "positive")]
    assert (await store.claims_counts()).get("grounded", 0) >= 1
```

Add to `tests/test_graph_api.py` next to `test_runtime_health_reports_outbox_pending_and_parked`, copying that test's construction of the runtime and fake store, and give the fake store:

```python
        async def claims_counts(self):
            return {"grounded": 3, "orphaned": 1}

        async def job_counts(self):
            return {"capture": {"pending": 2}}
```

and assert `payload["claims"] == {"grounded": 3, "orphaned": 1}` and `payload["jobs"] == {"capture": {"pending": 2}}`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_graph_api.py -q && pytest tests/integration/test_delete_spans.py -q -m integration --override-ini="addopts=" --migrate`
Expected: FAIL (`claims` missing from the payload; no `repair_drift`)

- [ ] **Step 3: Write the implementation**

`store_claims.py`:

```python
async def claims_counts_on_conn(conn) -> dict[str, int]:
    rows = await conn.fetch("SELECT coalesce(status, 'unmigrated') AS s, count(*)::int AS n FROM claims GROUP BY 1")
    return {r["s"]: int(r["n"]) for r in rows}


async def drift_keys(conn, limit: int = 500) -> set[TimelineKey]:
    """Keys whose stored rows disagree with their evidence (raw-SQL deletes, crashes)."""
    keys: set[TimelineKey] = set()
    rows = await conn.fetch(
        f"""
        SELECT c.owner, c.subject_id, c.verb, {OBJECT_KEY_SQL} AS object_key FROM claims c
         WHERE c.status IN ('grounded', 'unconfirmed') AND c.subject_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.owner = c.owner
                             AND e.claim_id = c.claim_id AND e.state = 'live')
        UNION
        SELECT e.owner, e.subject_id, e.verb, e.object_key FROM claim_evidence e
          JOIN claims c ON c.owner = e.owner AND c.claim_id = e.claim_id
         WHERE e.state = 'live' AND c.status = 'orphaned'
        LIMIT $1
        """, int(limit))
    for r in rows:
        keys.add(timeline_key(r["owner"], int(r["subject_id"]), await verb_row(conn, r["verb"]), r["object_key"]))
    return keys


async def repair_drift_on_conn(conn) -> int:
    keys = await drift_keys(conn)
    await rebuild_keys(conn, keys)
    return len(keys)
```

Mixin:

```python
    async def claims_counts(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            return await claims_counts_on_conn(conn)

    async def repair_drift(self) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await repair_drift_on_conn(conn)
```

`graph_runtime.py` `_adelivery_health`: after the outbox block add

```python
        extra: Dict[str, Any] = {}
        for key, name in (("claims", "claims_counts"), ("jobs", "job_counts")):
            fn = getattr(self.store, name, None)
            if callable(fn):
                try:
                    extra[key] = await fn()
                except Exception:
                    logger.warning("%s counts unavailable", key, exc_info=True)
                    extra[key] = None
```

and return `{"drain_status": …, "unpassported_count": …, "outbox": outbox, **extra}`.

`verify.py` `finally:` block: replace the `write_outbox` query with:

```python
        try:
            from .claims_migration import NAME as CLAIMS_MIGRATION, legacy_check
            from .store_claims import claims_counts_on_conn, repair_drift_on_conn
            from .store_jobs import JOB_MAX_ATTEMPTS

            row = await conn.fetchrow(
                "SELECT count(*) FILTER (WHERE status IN ('pending','leased','retryable') AND attempts < $1)::int "
                "AS pending, count(*) FILTER (WHERE status = 'failed' OR attempts >= $1)::int AS parked "
                "FROM durable_job WHERE kind = 'capture'", JOB_MAX_ATTEMPTS)
            result.notes.append(f"capture jobs pending={row['pending']} parked={row['parked']}")
            async with conn.transaction():
                repaired = await repair_drift_on_conn(conn)
            result.add("claims drift", True, f"rebuilt {repaired} key(s)")
            result.notes.append(f"claims by status {await claims_counts_on_conn(conn)}")
            applied = await conn.fetchval("SELECT 1 FROM data_migrations WHERE name = $1", CLAIMS_MIGRATION)
            check = await legacy_check(conn)
            result.add("claims migration", bool(applied) and not check["violations"],
                       f"applied={bool(applied)} violations={len(check['violations'])} "
                       f"superseded={len(check['superseded'])}")
        except Exception as exc:
            result.notes.append(f"claims/jobs checks unavailable ({type(exc).__name__}; apply V18/V19)")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest -q && pytest tests/integration -q -m integration --override-ini="addopts=" --migrate`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store_claims.py src/hermes_memory/graph_runtime.py src/hermes_memory/verify.py tests/test_graph_api.py tests/integration/test_delete_spans.py
git commit -m "feat(claims): health and verify report claims and jobs; verify repairs drift

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Phase 1 gate

Spec §10 Phase 1 gate: migration gate, timelines gate, reception unchanged except §5.3's listed changes.

**Files:**
- Modify: `CHANGELOG.md`, `README.md` (served-prompt statement), `docs/superpowers/plans/2026-11-14-graph-layer.md` (pointer)

- [ ] **Step 1: Full test run**

Run: `pytest -q && pytest tests/integration -q -m integration --override-ini="addopts=" --migrate && mypy src/hermes_memory`
Expected: all PASS; mypy clean, or no new errors compared with `git stash && mypy src/hermes_memory`.

- [ ] **Step 2: Eval harness regression** (eval container on 127.0.0.1:5470, password in `~/Documents/Codex/eval-pw.txt`; the harness refuses any database not named `*_eval`)

Run: `python scripts/eval_memory.py --dsn "postgresql://hermes:$(cat ~/Documents/Codex/eval-pw.txt)@127.0.0.1:5470/hermes_phase1_eval" --json > /tmp/claude-1000/phase1-eval.json`
Expected, compared with the Phase 0 baseline (spec §1): single-fact recall ≥ 0.95, cross-owner leakage 0, prefetch p95 ≤ 150 ms, multi-hop roughly unchanged (about 0.25; Phase 1 does not change retrieval).
Run: `python scripts/eval_memory.py --dsn "postgresql://hermes:$(cat ~/Documents/Codex/eval-pw.txt)@127.0.0.1:5470/hermes_phase1_eval" --durability 300 --json`
Expected: `rows_after_replay == expected_rows`, `duplicate_rows == 0`, `outbox_rows_left == 0`.

- [ ] **Step 3: Migration rehearsal on a copy of the installed database** (read-only on the live install; **ask the user before running**)

```bash
PGPASSWORD="$(grep HERMES_PG_PASSWORD ~/.hermes/plugins/hybrid-age/.env | cut -d= -f2)" \
  pg_dump -h 127.0.0.1 -p 5452 -U hermes -d hermes_memory_installed -Fc -f /tmp/claude-1000/installed.dump
PGPASSWORD="$(cat ~/Documents/Codex/eval-pw.txt)" createdb -h 127.0.0.1 -p 5470 -U hermes hermes_rehearsal_eval
PGPASSWORD="$(cat ~/Documents/Codex/eval-pw.txt)" pg_restore -h 127.0.0.1 -p 5470 -U hermes -d hermes_rehearsal_eval /tmp/claude-1000/installed.dump
HYBRID_AGE_DSN="postgresql://hermes:$(cat ~/Documents/Codex/eval-pw.txt)@127.0.0.1:5470/hermes_rehearsal_eval" python scripts/migrate.py
```

Then run the data migration and the check against the same DSN:

```bash
HYBRID_AGE_DSN="postgresql://hermes:$(cat ~/Documents/Codex/eval-pw.txt)@127.0.0.1:5470/hermes_rehearsal_eval" python - <<'PY'
import asyncio, json, os
import asyncpg
from hermes_memory.claims_migration import legacy_check, migrate_legacy_claims
from hermes_memory.store import Store

async def main():
    pool = await asyncpg.create_pool(os.environ["HYBRID_AGE_DSN"], min_size=1, max_size=2)
    print(json.dumps(await migrate_legacy_claims(Store(pool))))
    async with pool.acquire() as conn:
        print(json.dumps(await legacy_check(conn), default=str, indent=2))
    await pool.close()

asyncio.run(main())
PY
```
Expected: V18 and V19 apply; `violations == []`; `superseded` lists only newer-polarity resolutions. Delete the dump and the rehearsal database afterwards.

- [ ] **Step 4: Documentation**

`CHANGELOG.md`, Unreleased:

```markdown
### Changed
- Claims are owner-scoped and derived from evidence (`claim_evidence`); `status` replaces `valid`, which is now a read-only mirror. Contradictory statements resolve to the newer one instead of being held back.
- Functional facts (lives in, works for, based in, …) keep an event-time history; corrections restore the previous value.
- Write capture uses the new `durable_job` table; `write_outbox` is no longer written.
### Added
- Entities with alias equations and merges; span deletion API that rebuilds what the spans supported.
- Health and verify report claims by status, job counts, drift repair and the legacy migration check.
```

`README.md`: where the README describes what the provider injects, make sure one sentence says the served prompt is vector memory with neighbour and claim overlays and that graph traversal is not yet on the prompt path (spec §1). Add the sentence only if it is missing.

`docs/superpowers/plans/2026-11-14-graph-layer.md`: add under its Superseded banner: `Phase 1 plan: 2026-11-14-graph-layer-phase1.md. Phases 2 and 3 get their own plans once Phase 1's interfaces have shipped.`

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md README.md docs/superpowers/plans/2026-11-14-graph-layer.md
git commit -m "docs: graph layer Phase 1 changelog and served-prompt statement

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Report the gate**

Write the gate evidence (test counts, eval JSON summary, rehearsal check output) in the PR description. Do not open the PR or push without the user's go-ahead.
