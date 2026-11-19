"""Spec 5 T5.1: tool-digest capture from an OpenAI-style `messages` list
into `work_event`-shaped dicts.

This module owns its own copy of Hermes's identity-resolution logic
(never imports hermes-agent -- it is not a dependency of this repo). Keep
it in sync with docs/hermes-tool-call-identity-contract.md; a change
there should be reflected here, and tests/test_hermes_messages_contract.py
is the trip-wire that catches drift when a local hermes-agent checkout
is available.

Contract this module enforces (see the doc for the full citations):
  1. A tool-result row may legitimately arrive with no `_tool_call_uid`
     set (cross-batch case) -- resolve it by walking `messages` backwards
     to the nearest preceding assistant row naming the same tool_call_id,
     the same way Hermes's own `tool_call_uid_from_history` does. Pairing
     never crosses a `role == "user"` boundary.
  2. Two `tool_calls` entries sharing the literal same provider id INSIDE
     one assistant response collapse onto one minted `_tool_call_uid` --
     `source_event_id` must fold in the occurrence position (how many
     prior tool-result rows already matched this same owning-assistant +
     tool_call_id pair) so neither call is lost nor merged with the other.
  3. If no stable identity can be established at all, the event is
     skipped and counted under a reason code -- never given an invented
     id.
  4. The produced event dict NEVER carries raw tool output -- only a
     bounded summary (<=500 chars) and a sha256 hash of the full content,
     per the "no raw text in shadow-adjacent tables" rule.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Mapping, Optional

_TOOL_CALL_UID = "_tool_call_uid"
_TOOL_CALL_UIDS = "_tool_call_uids"

_SUMMARY_MAX_CHARS = 500


def _expand_id_variants(value: Any) -> frozenset:
    """Every wire spelling of one tool-call identifier: a composite
    "call_x|fc_y" id aliases both halves to one call (mirrors Hermes's
    `_expand_tool_id_variants`)."""
    variants: set = set()
    text = value.strip() if isinstance(value, str) else ""
    if text:
        variants.add(text)
        variants.update(p for p in (part.strip() for part in text.split("|")) if p)
    return frozenset(variants)


def _coalesce_tool_call_id(tc: Mapping[str, Any]) -> str:
    """Effective call id of a tool_calls entry; "" when none. Mirrors
    Hermes's `coalesce_tool_call_id`: prefer call_id, then id, and only
    the first half of a composite id."""
    for key in ("call_id", "id"):
        raw = tc.get(key)
        value = raw.strip() if isinstance(raw, str) else ""
        if value:
            return value.split("|", 1)[0].strip() or value
    return ""


def _tool_call_id_variants(tc: Mapping[str, Any]) -> frozenset:
    variants: set = set()
    for key in ("call_id", "id", "response_item_id"):
        variants |= _expand_id_variants(tc.get(key))
    return frozenset(variants)


def _index_tool_call_uids(assistant: Mapping[str, Any]) -> tuple[frozenset, dict[str, str]]:
    """(every pairing-id variant this assistant names, {variant: uid})."""
    calls = assistant.get("tool_calls") or ()
    named: set = set()
    for tc in calls:
        named |= _tool_call_id_variants(tc)
    uids = assistant.get(_TOOL_CALL_UIDS)
    index: dict[str, str] = {}
    if isinstance(uids, dict):
        for call_id, uid in uids.items():
            for variant in _expand_id_variants(call_id):
                index[variant] = uid
    return frozenset(named), index


def _resolve_uid_from_history(messages: list, tool_index: int) -> Optional[tuple[str, int]]:
    """(uid, owning_assistant_index) for the tool-result at `tool_index`,
    resolved from the nearest preceding assistant row naming its
    tool_call_id -- or None if unresolvable. Mirrors Hermes's
    `tool_call_uid_from_history`, including the user-turn boundary stop."""
    tool_call_id = messages[tool_index].get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    result_variants = _expand_id_variants(tool_call_id)
    for prior_index in range(tool_index - 1, -1, -1):
        prior = messages[prior_index]
        if not isinstance(prior, dict):
            continue
        if prior.get("role") == "user":
            return None  # a tool result never pairs across a user turn
        if prior.get("role") != "assistant":
            continue
        named, index = _index_tool_call_uids(prior)
        if result_variants.isdisjoint(named):
            continue
        for variant in result_variants:
            if variant in index:
                return index[variant], prior_index
        return None  # nearest naming assistant has no uid map (legacy row)
    return None


def _bounded_summary(tool_name: str, content: Any) -> str:
    """A structured, content-free digest -- counts only, like the shadow-
    table rule (C6: HMAC keys, ids, counts and latencies, never raw text).
    This is NOT a truncated excerpt of `content`: a prefix of real tool
    output is still real tool output, whatever its length, and upstream
    redaction (Hermes's own secret scrubbing) is not this module's safety
    net to rely on."""
    text = content if isinstance(content, str) else repr(content)
    char_count = len(text)
    name = tool_name or "unknown"
    summary = f"{name}: {char_count} chars"
    return summary[:_SUMMARY_MAX_CHARS]


def _content_hash(content: Any) -> str:
    text = content if isinstance(content, str) else repr(content)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_work_events(
    messages: list, *, owner: str, session_id: str, span_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """One work_event-shaped dict per resolvable tool occurrence in
    `messages`. Returns (events, skipped_count, skip_reasons). Never
    raises on malformed input -- an unparseable row is skipped and
    counted, same as an unresolvable identity."""
    events: list[dict[str, Any]] = []
    reasons: Counter = Counter()
    occurrence_counts: dict[tuple[int, str], int] = {}

    for index, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue

        tool_call_id = msg.get("tool_call_id")
        direct_uid = msg.get(_TOOL_CALL_UID)
        uid: Optional[str] = direct_uid if isinstance(direct_uid, str) and direct_uid else None

        owner_assistant_index: Optional[int] = None
        if isinstance(tool_call_id, str) and tool_call_id:
            resolved = _resolve_uid_from_history(messages, index)
            if resolved is not None:
                resolved_uid, owner_assistant_index = resolved
                uid = uid or resolved_uid

        if not uid:
            reasons["no_stable_key"] += 1
            continue

        # Occurrence discriminator: how many tool-result rows have already
        # consumed this exact (owning assistant row, tool_call_id) pair.
        # Falls back to the uid itself as the grouping key when no owning
        # assistant row was found (uid came from the row's own direct
        # _tool_call_uid with no resolvable tool_call_id) -- still correct,
        # since two such rows sharing one uid are the same hazard case.
        group_key = (owner_assistant_index if owner_assistant_index is not None else -1,
                     tool_call_id if isinstance(tool_call_id, str) else uid)
        occurrence_index = occurrence_counts.get(group_key, 0)
        occurrence_counts[group_key] = occurrence_index + 1

        content = msg.get("content")
        events.append({
            "owner": owner,
            "session_id": session_id,
            "span_id": span_id,
            "source_event_id": f"{uid}:{occurrence_index}",
            "tool": msg.get("name") or "unknown",
            "summary": _bounded_summary(msg.get("name") or "unknown", content),
            "content_hash": _content_hash(content),
        })

    return events, sum(reasons.values()), dict(reasons)
