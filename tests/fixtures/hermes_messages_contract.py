"""Hermes tool-call identity contract fixtures.

These are literal copies of the exact `messages` shapes hermes-agent's
`sync_turn`/`sync_all` path hands to a memory provider, per
docs/hermes-tool-call-identity-contract.md (verified against
~/.hermes/hermes-agent @ 8237c7f8ec179c88857f24a174509d985af704b7).
Keep these in sync with that doc -- if a scenario here no longer matches
reality, update the doc's cited line numbers in the same change.
"""
from __future__ import annotations

# Scenario 1: ordinary same-batch pairing. The tool result already carries
# tool_call_uid by the time sync_turn's messages snapshot is taken.
SIMPLE_PAIRED_BATCH: list[dict] = [
    {"role": "user", "content": "What's in config.py?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_abc123", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "config.py"}'}},
        ],
        "_tool_call_uids": {"call_abc123": "11111111111111111111111111111111"},
    },
    {
        "role": "tool",
        "tool_call_id": "call_abc123",
        "name": "read_file",
        "content": "DSN = ...",
        "_tool_call_uid": "11111111111111111111111111111111",
    },
    {"role": "assistant", "content": "It defines the DSN."},
]

# Scenario 2: cross-batch pairing. The assistant row landed in an EARLIER
# persisted batch and this snapshot's tool-result row has NOT been resolved
# yet -- no tool_call_uid key at all on the result. T5.1 must walk
# `messages` backwards itself (mirroring tool_call_uid_from_history) to find
# the nearest preceding assistant row naming this tool_call_id, rather than
# treating the missing key as "no stable identity."
CROSS_BATCH_UNRESOLVED: list[dict] = [
    {"role": "user", "content": "Run the test suite."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_def456", "type": "function",
             "function": {"name": "run_tests", "arguments": "{}"}},
        ],
        "_tool_call_uids": {"call_def456": "22222222222222222222222222222222"},
    },
    # <-- a persisted-batch boundary would fall here in the real system;
    # the result below arrives in a LATER snapshot without tool_call_uid set.
    {
        "role": "tool",
        "tool_call_id": "call_def456",
        "name": "run_tests",
        "content": "12 passed",
        # no "tool_call_uid" key at all -- this is the expected, not-a-bug state.
    },
]

# Scenario 3: the real identity hazard. One assistant response names the
# SAME literal provider id twice (a genuine duplicate within one batch, not
# cross-turn reuse). Both calls collapse onto ONE minted tool_call_uid.
# T5.1 must add an occurrence discriminator (e.g. list index) or correctly
# skip the second occurrence -- tool_call_uid alone cannot disambiguate them.
DUPLICATE_ID_WITHIN_ONE_RESPONSE: list[dict] = [
    {"role": "user", "content": "Check both files."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_0", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            {"id": "call_0", "type": "function",  # same literal id as above -- provider bug/quirk
             "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
        ],
        "_tool_call_uids": {"call_0": "33333333333333333333333333333333"},  # one uid for both
    },
    {"role": "tool", "tool_call_id": "call_0", "name": "read_file",
     "content": "contents of a.py", "_tool_call_uid": "33333333333333333333333333333333"},
    {"role": "tool", "tool_call_id": "call_0", "name": "read_file",
     "content": "contents of b.py", "_tool_call_uid": "33333333333333333333333333333333"},
]

# Scenario 4: provider id reuse ACROSS turns (not a same-batch collision).
# "call_0" is a legitimate, different occurrence each turn; a NEW collision
# within one session gets a deterministic `_d<n>` suffix by
# uniquify_tool_call_ids, but an id already seen is never rewritten.
REUSED_ID_ACROSS_TURNS: list[dict] = [
    {"role": "user", "content": "First, read a.py."},
    {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_0", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
        "_tool_call_uids": {"call_0": "44444444444444444444444444444444"},
    },
    {"role": "tool", "tool_call_id": "call_0", "name": "read_file",
     "content": "contents of a.py", "_tool_call_uid": "44444444444444444444444444444444"},
    {"role": "assistant", "content": "Done."},
    {"role": "user", "content": "Now read b.py."},
    {
        # same literal provider id "call_0" again, but this is a genuinely
        # new, distinct occurrence in a later turn -- uniquify_tool_call_ids
        # would have renamed this to "call_0_d1" before it ever reached
        # hybrid-age, because "call_0" is already in session history.
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_0_d1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}}],
        "_tool_call_uids": {"call_0_d1": "55555555555555555555555555555555"},
    },
    {"role": "tool", "tool_call_id": "call_0_d1", "name": "read_file",
     "content": "contents of b.py", "_tool_call_uid": "55555555555555555555555555555555"},
]

# Scenario 5: a bridged/composite id ("call_x|fc_y"). Only the "call_x" half
# is the pairing key (coalesce_tool_call_id splits on "|").
BRIDGED_COMPOSITE_ID: list[dict] = [
    {"role": "user", "content": "List the repo."},
    {
        "role": "assistant", "content": None,
        "tool_calls": [{"call_id": "call_ghi789|fc_response_item_42", "type": "function",
                         "function": {"name": "list_dir", "arguments": "{}"}}],
        "_tool_call_uids": {"call_ghi789": "66666666666666666666666666666666"},
    },
    {"role": "tool", "tool_call_id": "call_ghi789", "name": "list_dir",
     "content": "src/\ntests/", "_tool_call_uid": "66666666666666666666666666666666"},
]

# Scenario 6: no stable key at all -- a legacy row with no tool_calls uid
# minted and no tool_call_id on the result either (e.g. restored from a
# pre-identity-system export). T5.1 must skip this event with a reason
# code, never invent an identity.
NO_STABLE_KEY: list[dict] = [
    {"role": "user", "content": "Do something."},
    {"role": "assistant", "content": None,
     "tool_calls": [{"function": {"name": "legacy_tool", "arguments": "{}"}}]},  # no id/call_id at all
    {"role": "tool", "name": "legacy_tool", "content": "some output"},  # no tool_call_id either
]

ALL_SCENARIOS = {
    "simple_paired_batch": SIMPLE_PAIRED_BATCH,
    "cross_batch_unresolved": CROSS_BATCH_UNRESOLVED,
    "duplicate_id_within_one_response": DUPLICATE_ID_WITHIN_ONE_RESPONSE,
    "reused_id_across_turns": REUSED_ID_ACROSS_TURNS,
    "bridged_composite_id": BRIDGED_COMPOSITE_ID,
    "no_stable_key": NO_STABLE_KEY,
}
