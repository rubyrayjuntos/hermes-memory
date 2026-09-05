from hermes_memory.write_outcome import Kind, Stage, WriteLedger, WriteOutcome


def test_record_clips_password_in_detail():
    led = WriteLedger()
    stored = led.record(
        WriteOutcome(Stage.SQL_TURN, Kind.FAILED, detail="password=hunter2 leaked")
    )
    assert "hunter2" not in stored.detail
    assert "[redacted]" in stored.detail


def test_record_clips_exc_dsn():
    led = WriteLedger()
    stored = led.record(
        WriteOutcome(Stage.EMBED, Kind.EMBED_NULL),
        exc=RuntimeError("postgres://u:secret@localhost/db boom"),
    )
    assert "postgres://" not in stored.detail
    assert "secret" not in stored.detail


def test_dropped_keeps_session_id():
    led = WriteLedger()
    stored = led.record(
        WriteOutcome(Stage.ENQUEUE, Kind.DROPPED, session_id="sess-queue-full")
    )
    assert stored.session_id == "sess-queue-full"
    assert led.counts()["dropped_writes"] == 1


def test_failed_sql_increments_writes_failed():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.SQL_TURN, Kind.FAILED, detail="UndefinedColumn"))
    assert led.counts()["writes_failed"] == 1
    assert led.snapshot()["last_failed_stage"] == "sql_turn"


def test_embed_null_is_not_failed():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.EMBED, Kind.EMBED_NULL))
    assert led.counts()["embed_null"] == 1
    assert led.counts()["writes_failed"] == 0


def test_graph_degraded_after_ok_sql():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.SQL_TURN, Kind.OK, turn_id=1))
    led.record(WriteOutcome(Stage.FLOWER, Kind.GRAPH_DEGRADED))
    c = led.counts()
    assert c["writes_failed"] == 0
    assert c["graph_degraded"] == 1


def test_counts_always_four_int_keys():
    led = WriteLedger()
    c = led.counts()
    assert set(c) == {"dropped_writes", "writes_failed", "embed_null", "graph_degraded"}
    assert all(isinstance(v, int) for v in c.values())
    assert all(v == 0 for v in c.values())


def test_last_failed_only_updates_on_failed():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.EMBED, Kind.EMBED_NULL))
    led.record(WriteOutcome(Stage.FLOWER, Kind.GRAPH_DEGRADED))
    snap = led.snapshot()
    assert snap["last_failed_stage"] == ""
    assert snap["last_failed_at"] == ""
    led.record(WriteOutcome(Stage.MEMORY_SQL, Kind.FAILED, detail="UniqueViolation"))
    snap = led.snapshot()
    assert snap["last_failed_stage"] == "memory_sql"
    assert snap["last_failed_at"]


async def test_awrite_turn_insert_failure_records_writes_failed():
    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.write_outcome import LEDGER

    class BoomStore:
        async def insert_turn(self, *args, **kwargs):
            raise RuntimeError("insert_turn exploded")

    class StubEmbedder:
        async def embed_text(self, _text):
            return [0.0] * 768

    LEDGER.reset()
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider._agent_identity = "test-agent"
    provider._last_turn_id = {}
    await provider._awrite_turn(
        BoomStore(),
        StubEmbedder(),
        {"session_id": "s1", "content": "hello world", "role": "user"},
    )
    assert LEDGER.counts()["writes_failed"] == 1
    assert LEDGER.snapshot()["last_failed_stage"] == "sql_turn"


async def test_awrite_turn_flower_failure_is_graph_degraded():
    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.write_outcome import LEDGER

    class OkStore:
        async def insert_turn(self, *args, **kwargs):
            return 7

        async def fetch_noun_labels(self):
            return []

        async def write_noun_passports(self, *args, **kwargs):
            return []

    class StubEmbedder:
        async def embed_text(self, _text):
            return [0.0] * 768

    LEDGER.reset()
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider._agent_identity = "test-agent"
    provider._last_turn_id = {}

    async def boom_flower(*args, **kwargs):
        raise RuntimeError("flower MERGE failed")

    provider._link_turn_flower = boom_flower  # type: ignore[method-assign]
    await provider._awrite_turn(
        OkStore(),
        StubEmbedder(),
        {"session_id": "s1", "content": "hello world", "role": "user"},
    )
    c = LEDGER.counts()
    assert c["graph_degraded"] >= 1
    assert c["writes_failed"] == 0
