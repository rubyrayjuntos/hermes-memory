import datetime

from hermes_memory.extraction_jobs import job_specs, should_enqueue_model


def test_default_job_specs_only_enqueue_rules():
    specs = job_specs(42)
    assert specs == (
        ("extract_rules", "42:rules:5", {"span_id": 42, "extractor": "rules", "version": "5"}),
    )
    for _kind, _op, payload in specs:
        assert set(payload) == {"span_id", "extractor", "version"}


def test_model_filter_skips_questions_acknowledgements_and_code():
    assert not should_enqueue_model("Does Alice use Postgres?")
    assert not should_enqueue_model("Thanks.")
    assert not should_enqueue_model("```python\nprint('hello')\n```")


def test_model_filter_skips_rule_covered_facts_and_near_duplicates():
    assert not should_enqueue_model("Nightingale uses Atlas.")
    repeated = "Ray Swan is preparing the Hermes memory beta release."
    assert not should_enqueue_model(repeated, [repeated])
    assert not should_enqueue_model(
        "Ray Swan is preparing the Hermes memory beta release!",
        [repeated],
    )


def test_model_filter_keeps_unstructured_facts_and_uncovered_entities():
    assert should_enqueue_model("Ray Swan met Alice Chen at the 2026 AI Summit.")
    assert should_enqueue_model(
        "Nightingale uses Atlas and collaborates with Project Zephyr."
    )


def test_should_enqueue_model_when_first_person_cue_and_self_templates_miss():
    assert not should_enqueue_model("I live in NYC.")
    assert should_enqueue_model("these days I'm doing staff engineering")
    assert should_enqueue_model(
        "these days I'm doing staff engineering; Nightingale is a service"
    )


def test_model_filter_scores_authored_segments_only():
    paste = (
        "From: alice@example.com\n"
        "Subject: infra\n"
        "\n"
        "Ray Swan met Alice Chen at the 2026 AI Summit.\n"
    )
    assert not should_enqueue_model(paste)
    mixed = "Ray Swan met Alice Chen at the 2026 AI Summit.\n\n" + paste
    assert should_enqueue_model(mixed)


class _JobStore:
    def __init__(self, fail=False):
        self.calls = []
        self.batches = []
        self.paired_batches = []
        self.fail = fail

    async def job_add(self, owner, kind, op_id, payload, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((owner, kind, op_id, payload, kw))
        return (1, 0)

    async def job_add_model_batch(self, owner, session_id, span_id, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.batches.append((owner, session_id, span_id, kw))

    async def job_add_model_batch_paired(self, owner, session_id, span_id, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.paired_batches.append((owner, session_id, span_id, kw))


def _provider():
    from hermes_memory.provider import HybridAgeMemoryProvider

    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p._agent_identity = "me"
    return p


async def test_enqueue_defaults_to_rules_only_with_session_and_event_time():
    store = _JobStore()
    item = {"session_id": "s1", "captured_at": "2026-01-01T00:00:00Z", "agent_identity": "a"}
    await _provider()._enqueue_extraction_jobs(store, 42, item)
    assert [(c[0], c[1], c[2]) for c in store.calls] == [
        ("a", "extract_rules", "42:rules:5"),
    ]
    for _o, _k, _op, _payload, kw in store.calls:
        assert kw == {
            "session_id": "s1",
            "event_time": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            "priority": 0,
        }


async def test_enqueue_batches_only_eligible_user_turns_when_model_mode_is_configured():
    from types import SimpleNamespace

    store = _JobStore()
    provider = _provider()
    provider.config = SimpleNamespace(
        extract_mode="rules_then_model",
        extract_url="http://127.0.0.1:11434/v1",
        extract_model="llama3.2:3b",
    )
    await provider._enqueue_extraction_jobs(
        store, 42, {
            "session_id": "s",
            "agent_identity": "a",
            "role": "user",
            "content": "Ray Swan met Alice Chen at the 2026 AI Summit.",
        }
    )
    assert [call[1] for call in store.calls] == ["extract_rules"]
    assert store.batches == [("a", "s", 42, {"event_time": None})]


async def test_enqueue_does_not_batch_assistant_turns():
    from types import SimpleNamespace

    store = _JobStore()
    provider = _provider()
    provider.config = SimpleNamespace(
        extract_mode="rules_then_model",
        extract_url="http://127.0.0.1:11434/v1",
        extract_model="llama3.2:3b",
    )
    await provider._enqueue_extraction_jobs(
        store, 43, {
            "session_id": "s",
            "agent_identity": "a",
            "role": "assistant",
            "content": "Ray Swan met Alice Chen at the 2026 AI Summit.",
        }
    )
    assert store.batches == []


async def test_enqueue_pairs_assistant_reply_with_its_user_turn_when_flag_on():
    from types import SimpleNamespace

    store = _JobStore()
    provider = _provider()
    provider.config = SimpleNamespace(
        extract_mode="rules_then_model",
        extract_url="http://127.0.0.1:11434/v1",
        extract_model="llama3.2:3b",
        extract_assistant_replies=True,
    )
    await provider._enqueue_extraction_jobs(
        store, 43, {
            "session_id": "s",
            "agent_identity": "a",
            "role": "assistant",
            "content": "You prefer to work from home on Fridays, and I will remember that.",
            "previous_conversation_id": 42,
        }
    )
    assert store.batches == []
    assert store.paired_batches == [("a", "s", 43, {"paired_with": 42, "event_time": None})]


async def test_enqueue_still_skips_assistant_turns_when_flag_off():
    from types import SimpleNamespace

    store = _JobStore()
    provider = _provider()
    provider.config = SimpleNamespace(
        extract_mode="rules_then_model",
        extract_url="http://127.0.0.1:11434/v1",
        extract_model="llama3.2:3b",
        extract_assistant_replies=False,
    )
    await provider._enqueue_extraction_jobs(
        store, 43, {
            "session_id": "s",
            "agent_identity": "a",
            "role": "assistant",
            "content": "You prefer to work from home on Fridays, and I will remember that.",
            "previous_conversation_id": 42,
        }
    )
    assert store.batches == []
    assert store.paired_batches == []


def test_session_end_hook_logs_observation(caplog):
    from hermes_memory.provider import HybridAgeMemoryProvider

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = None
    provider._loop = None
    provider._agent_identity = ""
    provider._session_id = ""

    with caplog.at_level("INFO", logger="hybrid_age"):
        provider.on_session_end([])

    assert provider._session_end_callbacks == 1
    assert "session-end callback observed count=1" in caplog.text


async def test_enqueue_swallows_failure_and_skips_store_without_job_add():
    await _provider()._enqueue_extraction_jobs(_JobStore(fail=True), 1, {"session_id": "s"})
    await _provider()._enqueue_extraction_jobs(object(), 1, {"session_id": "s"})
