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
