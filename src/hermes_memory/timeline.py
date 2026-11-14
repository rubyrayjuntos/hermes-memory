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
