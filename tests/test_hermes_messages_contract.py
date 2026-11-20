"""Contract test for docs/hermes-tool-call-identity-contract.md.

Runs the fixtures in tests/fixtures/hermes_messages_contract.py through
hermes-agent's OWN identity functions (imported from a local checkout,
never vendored/copied) so a future Hermes change breaks THIS test
instead of silently corrupting what Spec 5's work-memory capture (T5.1)
extracts from `messages`.

hermes-agent is a sibling repo, not a dependency of hermes-memory, so
this test is skipped (not failed) when no local checkout is found --
exactly the existing pattern for the "nightly ollama e2e" tests that
need an environment this repo doesn't ship. When it DOES find a
checkout (the normal case in Ray's own dev environment), it runs for
real and is the actual enforcement mechanism the user asked for.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.fixtures.hermes_messages_contract import ALL_SCENARIOS

_CHECKOUT = Path(os.environ.get("HERMES_AGENT_CHECKOUT", os.path.expanduser("~/.hermes/hermes-agent")))
_AVAILABLE = (_CHECKOUT / "agent" / "message_metadata.py").is_file()

pytestmark = [
    pytest.mark.skipif(not _AVAILABLE, reason=f"no hermes-agent checkout at {_CHECKOUT}"),
]


@pytest.fixture(scope="module")
def hermes_identity():
    """Import hermes-agent's real identity functions from the sibling checkout."""
    sys.path.insert(0, str(_CHECKOUT))
    try:
        from agent.message_metadata import (  # type: ignore[import-not-found]
            TOOL_CALL_UID,
            TOOL_CALL_UIDS,
            index_tool_call_uids,
            resolve_tool_call_uid,
            tool_call_uid_from_history,
        )
        from agent.message_sanitization import coalesce_tool_call_id  # type: ignore[import-not-found]
    except ImportError as exc:
        # hermes-agent's own transitive deps (e.g. ruamel.yaml) aren't part of
        # hermes-memory's venv -- that is expected, not a reason to fail this
        # suite. Run this test from hermes-agent's own venv (or install its
        # deps) to get real enforcement; see the skip reason for how.
        pytest.skip(f"hermes-agent checkout present but its deps aren't importable here: {exc}")
    try:
        yield {
            "TOOL_CALL_UID": TOOL_CALL_UID,
            "TOOL_CALL_UIDS": TOOL_CALL_UIDS,
            "index_tool_call_uids": index_tool_call_uids,
            "resolve_tool_call_uid": resolve_tool_call_uid,
            "tool_call_uid_from_history": tool_call_uid_from_history,
            "coalesce_tool_call_id": coalesce_tool_call_id,
        }
    finally:
        sys.path.remove(str(_CHECKOUT))


def test_simple_paired_batch_already_has_a_resolved_uid(hermes_identity):
    messages = ALL_SCENARIOS["simple_paired_batch"]
    tool_row = next(m for m in messages if m.get("role") == "tool")
    assert tool_row.get(hermes_identity["TOOL_CALL_UID"]) == "11111111111111111111111111111111"


def test_cross_batch_result_has_no_uid_until_resolved_from_history(hermes_identity):
    """Fact #1 of the contract: a cross-batch tool result legitimately
    arrives with NO tool_call_uid. Hermes's own tool_call_uid_from_history
    resolves it by walking messages backwards -- which is exactly what
    T5.1 must do too, not treat the missing key as "no stable identity"."""
    messages = ALL_SCENARIOS["cross_batch_unresolved"]
    tool_index = next(i for i, m in enumerate(messages) if m.get("role") == "tool")
    assert messages[tool_index].get(hermes_identity["TOOL_CALL_UID"]) is None

    resolved = hermes_identity["tool_call_uid_from_history"](messages, tool_index, {})
    assert resolved == "22222222222222222222222222222222"


def test_duplicate_provider_id_within_one_response_collapses_to_one_uid(hermes_identity):
    """Fact #2 of the contract: two tool_calls entries with the identical
    literal provider id in ONE assistant response share ONE minted uid.
    tool_call_uid alone cannot disambiguate the two resulting events --
    T5.1 needs its own occurrence discriminator for this case."""
    messages = ALL_SCENARIOS["duplicate_id_within_one_response"]
    tool_rows = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_rows) == 2
    uids = {row.get(hermes_identity["TOOL_CALL_UID"]) for row in tool_rows}
    assert uids == {"33333333333333333333333333333333"}, (
        "expected both duplicate-id tool results to collapse onto the same uid -- "
        "if hermes-agent ever changes this, T5.1's discriminator logic needs revisiting"
    )


def test_reused_provider_id_across_turns_is_not_a_collision(hermes_identity):
    """Fact #2 continued: the SAME literal id appearing in a LATER turn is
    expected to have already been renamed by Hermes's own uniquifier, so
    by the time hybrid-age sees it, it is a distinct, stable id -- not an
    in-batch duplicate."""
    messages = ALL_SCENARIOS["reused_id_across_turns"]
    assistant_rows = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
    ids = [hermes_identity["coalesce_tool_call_id"](tc) for row in assistant_rows for tc in row["tool_calls"]]
    assert ids == ["call_0", "call_0_d1"], ids
    uids = {hermes_identity["coalesce_tool_call_id"](tc): row.get(hermes_identity["TOOL_CALL_UIDS"], {}).get(
        hermes_identity["coalesce_tool_call_id"](tc))
        for row in assistant_rows for tc in row["tool_calls"]}
    assert len(set(uids.values())) == 2, "two distinct turns must mint two distinct uids"


def test_bridged_composite_id_pairs_on_the_call_half_only(hermes_identity):
    """Fact #3 of the contract: a "call_x|fc_y" composite id's pairing key
    is only the call_x half."""
    messages = ALL_SCENARIOS["bridged_composite_id"]
    assistant_row = next(m for m in messages if m.get("role") == "assistant")
    tc = assistant_row["tool_calls"][0]
    assert hermes_identity["coalesce_tool_call_id"](tc) == "call_ghi789"


def test_no_stable_key_resolves_to_nothing(hermes_identity):
    """A legacy row with neither a minted uid nor a tool_call_id must
    resolve to None through every real Hermes path -- T5.1's "skip,
    don't invent" rule is the correct response, not a bug to work
    around."""
    messages = ALL_SCENARIOS["no_stable_key"]
    tool_index = next(i for i, m in enumerate(messages) if m.get("role") == "tool")
    assert messages[tool_index].get("tool_call_id") is None
    resolved = hermes_identity["tool_call_uid_from_history"](messages, tool_index, {})
    assert resolved is None
