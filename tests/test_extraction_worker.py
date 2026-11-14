"""Extraction runs on a worker task, never on the prompt path (spec §5.2)."""
import asyncio
import inspect

from hermes_memory import extraction_worker
from hermes_memory.provider import HybridAgeMemoryProvider


class FakeConn:
    def __init__(self, turns):
        # id -> (owner, session, content[, ts]); missing ts sorts as the id.
        self.turns = turns
        self.model_inputs = []

    def _parts(self, span_id):
        row = self.turns[span_id]
        owner, session, content = row[0], row[1], row[2]
        ts = row[3] if len(row) > 3 and row[3] is not None else span_id
        return owner, session, content, ts

    async def fetchrow(self, sql, *args):
        if "FROM durable_job" in sql:
            return {"owner": "o", "payload": {"span_id": 9}}
        span_id, owner = args[0], args[1]
        owner_, session, content, ts = self._parts(span_id)
        if owner_ != owner:
            return None
        return {"id": span_id, "session_id": session, "content": content, "role": "user", "ts": ts}

    async def fetch(self, sql, *args):
        if "FROM verb" in sql:
            return [{"verb": "uses"}, {"verb": "works_on"}]
        owner, session, span_ts, span_id, limit = args

        def key(i):
            return (self._parts(i)[3], i)

        rows = [(key(i), i, self._parts(i)[2]) for i, row in self.turns.items()
                if row[0] == owner and row[1] == session and key(i) < (span_ts, span_id)]
        rows.sort(reverse=True)
        rows = rows[:limit]
        return [{"id": i, "content": c} for _, i, c in rows]


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.open = 0  # connections currently held

    def acquire(self):
        conn = self.conn
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                pool.open += 1
                return conn

            async def __aexit__(self_inner, *exc):
                pool.open -= 1
                return False

        return _Ctx()


class FakeStore:
    def __init__(self, turns=None, jobs=None):
        self.claims = []
        self.done = []
        self.retried = []
        self.jobs = jobs if jobs is not None else {
            "extract_rules": [(1, 1, {"span_id": 9})],
            "extract_model": [(2, 1, {"span_id": 9})],
        }
        self.pool = FakePool(FakeConn(turns or {9: ("o", "s", "Nightingale uses Atlas.")}))

    async def job_claim(self, kind, worker, limit, *, lease_s, session_ordered=False,
                        defer_backfill=False, **kw):
        self.claims.append((kind, limit, lease_s, session_ordered, defer_backfill))
        return self.jobs.get(kind, [])[:limit]

    async def job_done(self, job_id, fence, *, delete=False):
        self.done.append((job_id, fence))
        return True

    async def job_retry(self, job_id, fence, error):
        self.retried.append((job_id, fence, error))
        return "retryable"


def _patch_publish(monkeypatch, rules=None, model=None):
    published = {"rules": [], "model": []}

    async def pub_rules(conn, embed, job_id, fence):
        published["rules"].append((job_id, fence, embed))
        if rules:
            await rules()
        return [1]

    async def pub_model(conn, embed, claims, job_id, fence):
        published["model"].append((job_id, fence, claims, embed))
        if model:
            await model()
        return [2]

    monkeypatch.setattr(extraction_worker, "publish_rules", pub_rules)
    monkeypatch.setattr(extraction_worker, "publish_model", pub_model)
    return published


async def _embed(texts):
    return [[0.0] for _ in texts]


async def test_claims_rules_then_model_with_lease_and_flags(monkeypatch):
    store = FakeStore()
    published = _patch_publish(monkeypatch)

    async def model(turn, context):
        return {"claims": []}

    n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert n == 2
    assert store.claims == [
        ("extract_rules", 1, 60, True, True),
        ("extract_model", 1, 120, True, True),
    ]
    assert published["rules"][0][:2] == (1, 1) and published["rules"][0][2] is _embed
    assert published["model"][0][:2] == (2, 1) and published["model"][0][3] is _embed
    assert store.done == [(2, 1)]  # publish_model does not settle; the worker does


async def test_no_pool_connection_is_held_during_the_model_call(monkeypatch):
    store = FakeStore(jobs={"extract_model": [(2, 1, {"span_id": 9})]})
    _patch_publish(monkeypatch)
    held = {}

    async def model(turn, context):
        held["during_model"] = store.pool.open
        return {"claims": []}

    n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert n == 1
    assert held["during_model"] == 0


async def test_empty_span_settles_job_outside_the_connection(monkeypatch):
    store = FakeStore(turns={9: ("o", "s", "   ")},
                      jobs={"extract_model": [(2, 1, {"span_id": 9})]})
    _patch_publish(monkeypatch)
    open_at_done = []
    real_done = store.job_done

    async def job_done(job_id, fence, **kw):
        open_at_done.append(store.pool.open)
        return await real_done(job_id, fence, **kw)

    store.job_done = job_done

    async def model(turn, context):
        raise AssertionError("model must not run for an empty span")

    n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert n == 1 and store.done == [(2, 1)]
    assert open_at_done == [0]


async def test_model_gets_four_previous_turns_truncated_and_grounds_claims(monkeypatch):
    turns = {i: ("o", "s", f"turn {i} " + "x" * 2000) for i in range(1, 7)}
    turns[9] = ("o", "s", "Nightingale uses Atlas.")
    turns[8] = ("other", "s", "foreign owner")
    turns[7] = ("o", "other-session", "foreign session")
    store = FakeStore(turns=turns, jobs={"extract_model": [(2, 1, {"span_id": 9})]})
    published = _patch_publish(monkeypatch)
    seen = {}

    async def model(turn, context):
        seen["turn"], seen["context"] = turn, list(context)
        return {"claims": [
            {"subject": "Nightingale", "verb": "uses", "object": "Atlas"},
            {"subject": "Nightingale", "verb": "bogus", "object": "Atlas"},
        ]}

    await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert seen["turn"] == "Nightingale uses Atlas."
    assert [c.split(" ")[1] for c in seen["context"]] == ["3", "4", "5", "6"]  # oldest first
    assert all(len(c) == 1000 for c in seen["context"])
    claims = published["model"][0][2]
    assert [(c["verb"]) for c in claims] == ["uses"]


async def test_model_context_follows_event_time_not_id_order(monkeypatch):
    # Higher ids with earlier event times must still be excluded when after the span in id order
    # only; context is the four prior *event-time* turns.
    turns = {
        1: ("o", "s", "turn early-id-late-time", 50),
        2: ("o", "s", "turn 2", 20),
        3: ("o", "s", "turn 3", 30),
        4: ("o", "s", "turn 4", 40),
        5: ("o", "s", "turn 5", 45),
        9: ("o", "s", "Nightingale uses Atlas.", 35),
    }
    store = FakeStore(turns=turns, jobs={"extract_model": [(2, 1, {"span_id": 9})]})
    _patch_publish(monkeypatch)
    seen = {}

    async def model(turn, context):
        seen["context"] = list(context)
        return {"claims": []}

    await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert [c.split(" ", 1)[1] for c in seen["context"]] == ["2", "3"]


async def test_failure_is_logged_not_raised_and_other_kind_still_runs(monkeypatch, caplog):
    store = FakeStore()

    async def boom():
        raise RuntimeError("embedder down")

    published = _patch_publish(monkeypatch, rules=boom)

    async def model(turn, context):
        return {"claims": []}

    with caplog.at_level("WARNING"):
        n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert n == 1
    assert store.retried and store.retried[0][:2] == (1, 1)
    assert published["model"]
    assert "extraction" in caplog.text


async def test_claim_failure_never_raises(monkeypatch):
    store = FakeStore()

    async def bad_claim(*a, **k):
        raise ConnectionError("db gone")

    store.job_claim = bad_claim
    n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=None)
    assert n == 0


def test_prefetch_does_not_call_run_extraction_once(monkeypatch):
    calls = {"n": 0}
    real = extraction_worker.run_extraction_once

    async def wrapped(*a, **k):
        calls["n"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(extraction_worker, "run_extraction_once", wrapped)

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)

    class Cfg:
        prefetch_timeout_s = 1.0

    provider.config = Cfg()

    async def fake_aprefetch(query):
        return "recall"

    provider._aprefetch = fake_aprefetch
    provider._run = lambda coro, timeout=3.0: asyncio.run(coro)

    assert provider.prefetch("what does Nightingale use?") == "recall"
    assert calls["n"] == 0
    assert "run_extraction_once" not in inspect.getsource(HybridAgeMemoryProvider.prefetch)
    assert "run_extraction_once" not in inspect.getsource(HybridAgeMemoryProvider._aprefetch)
