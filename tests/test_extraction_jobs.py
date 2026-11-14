from hermes_memory.extraction_jobs import job_specs


def test_a_new_span_enqueues_rules_then_model_with_ids_only():
    specs = job_specs(42)
    assert specs == (
        ("extract_rules", "42:rules:1", {"span_id": 42, "extractor": "rules", "version": "1"}),
        ("extract_model", "42:model:1", {"span_id": 42, "extractor": "model", "version": "1"}),
    )
    for _kind, _op, payload in specs:
        assert set(payload) == {"span_id", "extractor", "version"}


class _JobStore:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def job_add(self, owner, kind, op_id, payload, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((owner, kind, op_id, payload, kw))
        return (1, 0)


def _provider():
    from hermes_memory.provider import HybridAgeMemoryProvider

    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p._agent_identity = "me"
    return p


async def test_enqueue_adds_both_jobs_with_session_and_event_time():
    store = _JobStore()
    item = {"session_id": "s1", "captured_at": "2026-01-01T00:00:00Z", "agent_identity": "a"}
    await _provider()._enqueue_extraction_jobs(store, 42, item)
    assert [(c[0], c[1], c[2]) for c in store.calls] == [
        ("a", "extract_rules", "42:rules:1"),
        ("a", "extract_model", "42:model:1"),
    ]
    for _o, _k, _op, _payload, kw in store.calls:
        assert kw == {"session_id": "s1", "event_time": "2026-01-01T00:00:00Z", "priority": 0}


async def test_enqueue_swallows_failure_and_skips_store_without_job_add():
    await _provider()._enqueue_extraction_jobs(_JobStore(fail=True), 1, {"session_id": "s"})
    await _provider()._enqueue_extraction_jobs(object(), 1, {"session_id": "s"})
