"""Extraction runs on a worker task, never on the prompt path (spec §5.2)."""
import asyncio
import inspect

from hermes_memory import extraction_worker
from hermes_memory.provider import HybridAgeMemoryProvider


class FakeConn:
    def __init__(self, turns, payload=None):
        self.turns = turns
        self.payload = payload or {"span_ids": [9]}

    def _parts(self, span_id):
        row = self.turns[span_id]
        owner, session, content = row[0], row[1], row[2]
        role = row[3] if len(row) > 3 else "user"
        return owner, session, content, role

    async def fetchrow(self, sql, *args):
        if "FROM durable_job" in sql:
            return {"owner": "o", "session_id": "s", "payload": self.payload}
        span_id, owner = args[0], args[1]
        owner_, session, content, role = self._parts(span_id)
        if owner_ != owner:
            return None
        return {"id": span_id, "session_id": session, "content": content, "role": role}

    async def fetch(self, sql, *args):
        if "FROM verb" in sql:
            return [{"verb": "uses"}, {"verb": "works_on"}]
        return []


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
        model_jobs = self.jobs.get("extract_model", [])
        payload = model_jobs[0][2] if model_jobs else None
        self.pool = FakePool(
            FakeConn(turns or {9: ("o", "s", "Nightingale uses Atlas.")}, payload)
        )
        self.model_calls = []
        self.model_tokens = []

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

    async def job_record_model_call(self, job_id, fence):
        self.model_calls.append((job_id, fence))
        return True

    async def job_record_model_tokens(self, job_id, fence, prompt_tokens, completion_tokens):
        self.model_tokens.append((job_id, fence, prompt_tokens, completion_tokens))
        return True


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

    async def model(batch):
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

    async def model(batch):
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

    async def model(batch):
        raise AssertionError("model must not run for an empty span")

    n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert n == 1 and store.done == [(2, 1)]
    assert open_at_done == [0]


async def test_batch_includes_rule_findings_and_only_grounds_claims_to_source_turn(monkeypatch):
    turns = {
        9: ("o", "s", "Nightingale uses Atlas."),
        10: ("o", "s", "Rhea works_on Project Zephyr."),
    }
    store = FakeStore(
        turns=turns,
        jobs={"extract_model": [(2, 1, {"span_ids": [9, 10]})]},
    )
    published = _patch_publish(monkeypatch)
    seen = {}

    async def model(batch):
        seen["batch"] = batch
        return {"claims": [
            {"span_id": 9, "subject": "Nightingale", "verb": "uses", "object": "Atlas"},
            {"span_id": 10, "subject": "Rhea", "verb": "works_on", "object": "Project Zephyr"},
            {"span_id": 9, "subject": "Nightingale", "verb": "uses", "object": "Project Zephyr"},
            {"span_id": 99, "subject": "Nightingale", "verb": "uses", "object": "Atlas"},
        ]}

    await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert [turn["span_id"] for turn in seen["batch"].turns] == [9, 10]
    assert [finding["span_id"] for finding in seen["batch"].rule_findings] == [9]
    claims = published["model"][0][2]
    assert [(c["span_id"], c["verb"]) for c in claims] == [(9, "uses"), (10, "works_on")]


async def test_failure_is_logged_not_raised_and_other_kind_still_runs(monkeypatch, caplog):
    store = FakeStore()

    async def boom():
        raise RuntimeError("embedder down")

    published = _patch_publish(monkeypatch, rules=boom)

    async def model(batch):
        return {"claims": []}

    with caplog.at_level("WARNING"):
        n = await extraction_worker.run_extraction_once(object(), store, embed=_embed, model=model)

    assert n == 1
    assert store.retried and store.retried[0][:2] == (1, 1)
    assert published["model"]
    assert "extraction" in caplog.text
    assert store.model_calls == [(2, 1)]


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
