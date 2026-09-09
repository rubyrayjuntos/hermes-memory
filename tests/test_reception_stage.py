"""Tests for _reception_stage: user-only claims/aliases, uptake, repair retraction."""
import pytest


class FakeStore:
    def __init__(self, prior=None):
        self.prior = prior
        self.aliases = []
        self.claim_inserts = []
        self.uptakes = []
        self.retractions = []
        self._ids = iter(range(1000, 2000))

    async def insert_alias(self, surface, canon, source):
        self.aliases.append((surface, canon, source))
        return True

    async def insert_claims(self, span_id, claims):
        self.claim_inserts.append((span_id, claims))
        return [next(self._ids) for _ in claims]

    async def previous_turn(self, session_id, conv_id, *, role="assistant"):
        return self.prior

    async def write_uptake(self, prior_id, next_id, value):
        self.uptakes.append((prior_id, next_id, value))

    async def retract_on_repair(self, prior_id, next_id, text, claims):
        self.retractions.append((prior_id, next_id, text, claims))
        return 1


def make_provider():
    from hermes_memory.provider import HybridAgeMemoryProvider

    return HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)


async def test_assistant_turn_writes_nothing():
    store = FakeStore(prior={"id": 1, "content": "hello"})
    await make_provider()._reception_stage(store, 2, "s", "assistant", "Tokyo Eye uses X.")
    assert store.aliases == [] and store.claim_inserts == []
    assert store.uptakes == [] and store.retractions == []


async def test_user_repair_flow():
    store = FakeStore(prior={"id": 10, "content": "Nightingale uses Atlas."})
    p = make_provider()
    await p._reception_stage(
        store, 11, "s", "user", "No, I meant Tokyo Eye. Tokyo Eye uses Poincare geometry.")
    assert store.uptakes == [(10, 11, "repaired")]
    assert len(store.retractions) == 1
    assert store.claim_inserts[0][0] == 11
    subjects = [c["subject"] for c in store.claim_inserts[0][1]]
    assert "Tokyo Eye" in subjects


async def test_user_plain_flow_no_retract():
    store = FakeStore(prior={"id": 10, "content": "ok"})
    await make_provider()._reception_stage(
        store, 11, "s", "user", "let's apply first to HPC S2S")
    assert store.uptakes == [(10, 11, "unknown")]
    assert store.retractions == []
    assert store.claim_inserts == []  # no supported verb → span only


async def test_alias_equation_from_user():
    store = FakeStore(prior=None)
    await make_provider()._reception_stage(
        store, 5, "s", "user", "The rig (aka Nightingale) is down.")
    assert ("nightingale", "the rig", "user_span") in store.aliases


async def test_reception_never_raises():
    class Boom(FakeStore):
        async def previous_turn(self, *a, **k):
            raise RuntimeError("db gone")

    p = make_provider()
    await p._reception_stage(Boom(), 1, "s", "user", "No, wrong.")
    await p._reception_stage(Boom(), 1, "s", "assistant", "x" * 10)
    await p._reception_stage(Boom(), 1, "s", "user", "   ")
