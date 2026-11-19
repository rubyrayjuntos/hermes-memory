"""Spec 5 T5.1: tool-digest capture from `messages` into work_event rows.

Pure-function tests -- no DB. Exercises src/hermes_memory/work_capture.py
against the literal Hermes message shapes in
tests/fixtures/hermes_messages_contract.py (the contract verified in
docs/hermes-tool-call-identity-contract.md). Never asserts on raw tool
output appearing anywhere in a produced event.
"""
from __future__ import annotations

import hashlib

from hermes_memory.work_capture import compute_work_events
from tests.fixtures.hermes_messages_contract import ALL_SCENARIOS


def test_duplicate_id_within_one_response_produces_two_distinct_events():
    """Two tool_calls entries sharing the literal same provider id inside
    ONE assistant response must not collapse onto one event -- tool_call_uid
    alone can't disambiguate them, so the occurrence position in that
    response is folded into source_event_id."""
    events, skipped, reasons = compute_work_events(
        ALL_SCENARIOS["duplicate_id_within_one_response"], owner="o", session_id="s",
    )
    assert skipped == 0, reasons
    assert len(events) == 2, "both tool results must survive as separate events"
    source_ids = {e["source_event_id"] for e in events}
    assert len(source_ids) == 2, "the two events must not share a source_event_id"
    # Both share the same base uid (that's the hazard) but differ by occurrence index.
    base_uids = {sid.rsplit(":", 1)[0] for sid in source_ids}
    assert base_uids == {"33333333333333333333333333333333"}
    occurrence_suffixes = {sid.rsplit(":", 1)[1] for sid in source_ids}
    assert occurrence_suffixes == {"0", "1"}
    # Neither call may be silently dropped: distinguish by content hash (distinct bodies).
    content_hashes = {e["content_hash"] for e in events}
    assert len(content_hashes) == 2, "distinct tool outputs must hash to distinct values"


def test_missing_uid_is_resolved_by_walking_messages_backwards():
    """Fact #1 of the contract: a cross-batch tool result legitimately
    arrives with no tool_call_uid. compute_work_events must resolve it the
    same way Hermes's own tool_call_uid_from_history does, not treat the
    missing key as unpairable."""
    events, skipped, reasons = compute_work_events(
        ALL_SCENARIOS["cross_batch_unresolved"], owner="o", session_id="s",
    )
    assert skipped == 0, reasons
    assert len(events) == 1
    assert events[0]["source_event_id"].startswith("22222222222222222222222222222222")


def test_unresolvable_uid_is_skipped_and_counted_not_invented():
    """A legacy row with neither a minted uid nor a usable tool_call_id must
    be skipped with a reason code, never assigned a made-up identity."""
    events, skipped, reasons = compute_work_events(
        ALL_SCENARIOS["no_stable_key"], owner="o", session_id="s",
    )
    assert events == []
    assert skipped == 1
    assert reasons.get("no_stable_key") == 1


def test_reused_id_across_turns_is_not_treated_as_a_duplicate():
    """Two DIFFERENT turns' tool calls, already distinctly renamed by
    Hermes's own uniquifier before reaching hybrid-age, must produce two
    independent events with independent source_event_ids -- not be folded
    together as if they were a same-response duplicate."""
    events, skipped, reasons = compute_work_events(
        ALL_SCENARIOS["reused_id_across_turns"], owner="o", session_id="s",
    )
    assert skipped == 0, reasons
    assert len(events) == 2
    assert len({e["source_event_id"] for e in events}) == 2


def test_bridged_composite_id_resolves_and_captures_one_event():
    events, skipped, reasons = compute_work_events(
        ALL_SCENARIOS["bridged_composite_id"], owner="o", session_id="s",
    )
    assert skipped == 0, reasons
    assert len(events) == 1
    assert events[0]["source_event_id"].startswith("66666666666666666666666666666666")


def test_work_event_never_carries_raw_tool_output():
    """Only a bounded, content-free summary and the hash of the full
    content may appear -- never an excerpt of the real tool output, even
    a short one, and even if Hermes's own upstream secret-redaction had
    somehow not already run."""
    huge_output = "SECRET_TOKEN_abc123 " + ("x" * 5000)
    messages = [
        {"role": "user", "content": "Dump the huge output."},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_huge", "type": "function",
                             "function": {"name": "dump", "arguments": "{}"}}],
            "_tool_call_uids": {"call_huge": "77777777777777777777777777777777"},
        },
        {"role": "tool", "tool_call_id": "call_huge", "name": "dump",
         "content": huge_output, "_tool_call_uid": "77777777777777777777777777777777"},
    ]
    events, skipped, reasons = compute_work_events(messages, owner="o", session_id="s")
    assert skipped == 0, reasons
    assert len(events) == 1
    event = events[0]

    assert "SECRET_TOKEN_abc123" not in str(event)
    assert "x" * 100 not in str(event)
    assert len(event["summary"]) <= 500
    assert event["content_hash"] == hashlib.sha256(huge_output.encode("utf-8")).hexdigest()
    # The event dict itself must not even carry a "content"/"raw" field as a landmine for a future caller.
    assert "content" not in event
    assert "raw" not in event
    assert "raw_content" not in event


def test_summary_is_never_a_content_excerpt_even_when_short():
    """A short, harmless-looking tool output must still never appear
    verbatim (or as any substring) in the summary -- the summary is a
    structured, content-free digest (tool name + length), not a
    truncated-but-still-raw prefix."""
    messages = [
        {"role": "user", "content": "Read a tiny file."},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_tiny", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}],
            "_tool_call_uids": {"call_tiny": "88888888888888888888888888888888"},
        },
        {"role": "tool", "tool_call_id": "call_tiny", "name": "read_file",
         "content": "DSN=postgres://user:hunter2@host/db",
         "_tool_call_uid": "88888888888888888888888888888888"},
    ]
    events, skipped, reasons = compute_work_events(messages, owner="o", session_id="s")
    assert skipped == 0, reasons
    assert "hunter2" not in events[0]["summary"]
    assert "DSN" not in events[0]["summary"]
    assert "postgres://" not in events[0]["summary"]


def test_simple_paired_batch_produces_one_event_with_tool_name():
    events, skipped, reasons = compute_work_events(
        ALL_SCENARIOS["simple_paired_batch"], owner="o", session_id="s",
    )
    assert skipped == 0, reasons
    assert len(events) == 1
    assert events[0]["tool"] == "read_file"
    assert events[0]["owner"] == "o"
    assert events[0]["session_id"] == "s"
