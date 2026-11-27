"""Regression test for a silent, irrecoverable write-drop under sustained
write pressure.

Found during the LongMemEval benchmark's ingest-timing dry run
(2026-11-26): bulk-replaying one question's ~491-turn haystack through
sync_turn() at full speed (no pacing -- the realistic shape of any fast
bulk write, or a very fast / very long real session) silently dropped
422 of 491 turns. Root cause: sync_turn()'s write queue (default
maxsize=256) fills faster than the background drain loop (embeddings +
rule extraction + graph writes) can keep up, and _enqueue_write() only
degrades gracefully if the item was FIRST durably recorded to the outbox
(_record_outbox() -> store.outbox_add()) -- but that outbox write goes
through the SAME connection pool that is also busy draining the queue,
so under real sustained pressure the durability safety net can itself
be starved at exactly the moment it's needed. When outbox_add() doesn't
land in time, the item has no _outbox_id, and a later QueueFull hits the
*irrecoverable* path: self._dropped_writes += 1, logger.warning() only
(easy to miss in production logs), no exception raised, no caller-visible
signal, no automatic retry.

This is a product bug, not a benchmark-harness artifact: any real agent
session that writes turns faster than hermes-memory can drain them (a
burst of fast tool-call turns, a bulk import, a very long autonomous
run) hits the same path and can lose memories with zero visible error.

Known issue, not fixed here -- tracked for a future release. See the
GitHub issue filed alongside this test.
"""
from __future__ import annotations

import pytest

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider


def _assert_hermes_test_dsn(dsn: str) -> None:
    assert "hermes_test" in dsn or "_eval" in dsn, (
        f"refusing to run against non-test DSN: {dsn!r}"
    )


@pytest.mark.integration
def test_rapid_bulk_sync_turn_does_not_silently_drop_writes(
    hermes_test_dsn, clean_hermes_test_db, migrated_db,
):
    """Replaying many turns back-to-back with NO pacing (the realistic
    shape of a fast burst or bulk import) must not silently lose any of
    them. Today it does: this reproduces the bug tests-first.
    """
    _assert_hermes_test_dsn(hermes_test_dsn)

    owner = "queue-full-drop-regression"
    n_turns = 200
    # A small queue_maxsize makes QueueFull deterministic regardless of
    # how fast this particular machine's drain loop happens to run --
    # the point under test is the *handling* of QueueFull, not whether
    # we can time a race against it.
    cfg = HybridAgeConfig(dsn=hermes_test_dsn, work_capture=False,
                           extract_mode="rules_only", queue_maxsize=8)
    provider = HybridAgeMemoryProvider(config=cfg)
    provider.initialize(session_id=f"{owner}-s0", agent_identity=owner)
    try:
        for i in range(n_turns):
            # Content long enough and distinctive enough to never be
            # noise-filtered, isolating this test to the queue-drop path.
            provider.sync_turn(
                f"user turn number {i} needs at least forty characters of real content",
                f"assistant turn number {i} also needs at least forty characters of real content",
                session_id=f"{owner}-s0",
            )
            # Deliberately NO sleep/backpressure here -- that is the point.

        loop = provider._loop
        queue = provider._write_queue
        if loop is not None and queue is not None:
            import asyncio
            asyncio.run_coroutine_threadsafe(queue.join(), loop).result(timeout=120)
    finally:
        # Review finding, PR #121: initialize() starts a dedicated
        # event-loop thread, connection pool, drain task, and periodic
        # outbox-replay task; queue.join() does not stop any of those.
        # An un-shut-down provider can replay deferred outbox rows
        # AFTER clean_hermes_test_db truncates tables for the next
        # test, contaminating it. Always shut down before teardown,
        # even if an assertion above raised.
        if hasattr(provider, "shutdown"):
            provider.shutdown()

    assert provider._dropped_writes == 0, (
        f"BUG: {provider._dropped_writes} writes were silently, irrecoverably "
        "dropped under sustained write pressure (QueueFull with no outbox "
        "fallback available in time). A real session hitting this burst "
        "shape loses memories with only a logger.warning() -- no exception, "
        "no caller-visible signal. See issue."
    )


@pytest.mark.integration
def test_queue_full_without_outbox_fallback_drops_silently_with_no_exception(
    hermes_test_dsn, clean_hermes_test_db, migrated_db, monkeypatch,
):
    """The narrower, more realistic failure: under genuine sustained load
    the SAME connection pool backs both the drain loop and
    store.outbox_add(), so the durability safety net can itself be
    starved exactly when it's needed. Simulate that directly by making
    outbox_add() unavailable (as if it timed out under load) and confirm
    what actually happens: a hard, silent drop with no exception raised
    to the caller and no loud signal -- only a single logger.warning()
    line that's trivial to miss in production logs, and no caller-visible
    way to know data was lost. This is the "at least log loudly or block"
    gap the issue asks to close.
    """
    _assert_hermes_test_dsn(hermes_test_dsn)

    owner = "queue-full-no-outbox-regression"
    cfg = HybridAgeConfig(dsn=hermes_test_dsn, work_capture=False,
                           extract_mode="rules_only", queue_maxsize=2)
    provider = HybridAgeMemoryProvider(config=cfg)
    provider.initialize(session_id=f"{owner}-s0", agent_identity=owner)

    # Simulate the durability layer being starved: outbox_add() is
    # unreachable in time, exactly the condition _record_outbox()'s own
    # docstring says degrades to "write kept in memory only".
    store = provider.store
    if store is not None and hasattr(store, "outbox_add"):
        async def _unavailable(*_a, **_k):
            raise TimeoutError("simulated: outbox_add starved under load")
        monkeypatch.setattr(store, "outbox_add", _unavailable)

    raised = []
    import logging
    logger = logging.getLogger("hybrid_age")
    log_records = []
    handler = logging.Handler()
    handler.emit = lambda record: log_records.append(record)
    logger.addHandler(handler)
    try:
        for i in range(50):
            try:
                provider.sync_turn(
                    f"user turn number {i} needs at least forty characters of real content",
                    f"assistant turn number {i} also needs at least forty characters of real content",
                    session_id=f"{owner}-s0",
                )
            except Exception as exc:  # pragma: no cover -- document if this ever changes
                raised.append(exc)

        loop = provider._loop
        queue = provider._write_queue
        if loop is not None and queue is not None:
            import asyncio
            try:
                asyncio.run_coroutine_threadsafe(queue.join(), loop).result(timeout=30)
            except Exception:
                pass
    finally:
        logger.removeHandler(handler)
        # Review finding, PR #121: same teardown gap as the first test --
        # shut down before clean_hermes_test_db truncates for the next
        # test, even if an assertion raised.
        if hasattr(provider, "shutdown"):
            provider.shutdown()

    # Accept ANY of the fix shapes the issue explicitly allows, instead
    # of requiring the data-loss behavior to remain (review finding,
    # PR #121): a drop still happening with no caller-visible signal is
    # the ONLY outcome this test should reject.
    if raised:
        # Fix made the drop caller-visible via an exception -- the gap
        # this test guards against ("no exception, no caller-visible
        # signal") is closed. Nothing more to check.
        return
    if provider._dropped_writes == 0:
        # Fix used blocking/backpressure to avoid the drop entirely --
        # also closes the gap. Nothing more to check.
        return

    # A drop happened and nothing was raised to the caller -- the only
    # remaining acceptable outcome is a loud enough log to notice it.
    drop_records = [r for r in log_records if "queue full" in r.getMessage()]
    assert drop_records, "expected at least one 'queue full' log record"
    assert any(r.levelno >= logging.ERROR for r in drop_records), (
        f"BUG: irrecoverable write drops are only logged at "
        f"{logging.getLevelName(drop_records[0].levelno)}, not ERROR+ -- "
        "easy to miss in production. Fix: raise the log level to ERROR "
        "(or implement blocking/backpressure instead of dropping), per "
        "the issue filed alongside this test."
    )
