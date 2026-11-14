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
