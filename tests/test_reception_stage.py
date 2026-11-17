"""Tests for _reception_stage: user-only claims/aliases, uptake, repair retraction."""


class FakeStore:
    def __init__(self, prior=None):
        self.prior = prior
        self.aliases = []
        self.claim_inserts = []
        self.uptakes = []
        self.retractions = []
        self.stamped = []
        self.pending = []
        self._ids = iter(range(1000, 2000))

    async def record_alias_equation(self, span_id: int, canon: str, surface: str) -> bool:
        self.aliases.append((surface, canon, "user_span"))
        return True

    async def record_rule_claims(self, span_id: int, claims: list, **kw) -> list:
        self.claim_inserts.append((span_id, claims, kw))
        return [next(self._ids) for _ in claims]

    async def previous_turn(
        self, session_id: str, conv_id: int, *, role: str = "assistant"
    ):
        return self.prior

    async def write_uptake(self, prior_id: int, next_id: int, value: str) -> None:
        self.uptakes.append((prior_id, next_id, value))

    async def record_repair(
        self, prior_id: int, next_id: int, *, deictic: bool, new_claims=()
    ) -> int:
        self.retractions.append((prior_id, next_id, deictic, list(new_claims)))
        return 1

    async def stamp_claims_status(self, conv_id: int, status: str) -> None:
        self.stamped.append((conv_id, status))

    async def fetch_pending_reception(self, limit: int) -> list:
        return list(self.pending)[:limit]


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


async def test_deictic_repair_without_new_claims_is_flagged_deictic():
    store = FakeStore(prior={"id": 10, "content": "Nightingale uses Atlas."})
    await make_provider()._reception_stage(store, 11, "s", "user", "No, not that.")
    assert store.retractions == [(10, 11, True, [])]


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


async def test_stage_stamps_ready_and_none():
    store = FakeStore(prior={"id": 10, "content": "ok"})
    p = make_provider()
    await p._reception_stage(store, 11, "s", "user", "No, wrong.")
    assert (11, "ready") in store.stamped
    await p._reception_stage(store, 12, "s", "assistant", "fine")
    assert (12, "none") in store.stamped


async def test_catch_up_reaps_pending():
    store = FakeStore(prior=None)
    store.pending = [
        {"id": 7, "session_id": "s", "role": "user",
         "content": "Deloitte uses Azure."}
    ]
    n = await make_provider().catch_up_reception(store, limit=10)
    assert n == 1
    assert (7, "ready") in store.stamped
    assert store.claim_inserts[0][0] == 7


PASTED_EMAIL = (
    "From: alice@example.com\n"
    "Subject: ownership\n"
    "\n"
    "Bob is the owner.\n"
    "> quoted leftover\n"
    "-- \n"
    "Sent from my iPhone\n"
    "No, I meant Friday.\n"
)


def _user_claim_batches(store):
    return [
        claims for _span, claims, kw in store.claim_inserts
        if kw.get("speaker") in (None, "user")
    ]


def _document_claim_batches(store):
    return [
        (claims, kw) for _span, claims, kw in store.claim_inserts
        if kw.get("speaker") == "document"
    ]


async def test_pasted_email_writes_no_user_claims_or_repairs():
    store = FakeStore(prior={"id": 10, "content": "Meeting is Thursday."})
    await make_provider()._reception_stage(store, 11, "s", "user", PASTED_EMAIL)
    assert _user_claim_batches(store) == []
    assert store.retractions == []
    assert store.uptakes == [(10, 11, "unknown")]
    docs = _document_claim_batches(store)
    assert docs, "pasted assertion may be recorded as document evidence"
    subjects = [c["subject"] for claims, _kw in docs for c in claims]
    assert "Bob" in subjects
    for _claims, kw in docs:
        assert kw.get("extractor") == "rules_document"
        assert kw.get("seg_start") is not None and kw.get("seg_end") is not None


async def test_code_fence_does_not_extract_assertions():
    store = FakeStore(prior=None)
    await make_provider()._reception_stage(
        store, 11, "s", "user", "```\nConfig uses Redis\n```")
    assert store.claim_inserts == []


async def test_mixed_turn_extracts_authored_user_claim_and_document_paste():
    text = "Nightingale uses Postgres. Can you clean this up?\n\n" + PASTED_EMAIL
    store = FakeStore(prior=None)
    await make_provider()._reception_stage(store, 11, "s", "user", text)
    user_subjects = [c["subject"] for claims in _user_claim_batches(store) for c in claims]
    assert user_subjects == ["Nightingale"]
    docs = _document_claim_batches(store)
    assert docs
    for claims, kw in docs:
        assert kw.get("speaker") == "document"
        assert all(c["subject"] != "Nightingale" for c in claims)


async def test_short_authored_repair_still_fires():
    store = FakeStore(prior={"id": 10, "content": "Spot is a labrador."})
    await make_provider()._reception_stage(
        store, 11, "s", "user", "No, I meant Spot is a poodle.")
    assert store.uptakes == [(10, 11, "repaired")]
    assert len(store.retractions) == 1
