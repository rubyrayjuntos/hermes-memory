# Hermes tool-call identity contract (verified against hermes-agent)

**Verified against:** local checkout `~/.hermes/hermes-agent` @ commit
`8237c7f8ec179c88857f24a174509d985af704b7` (2026-10-22). This is the exact
same commit the earlier 2026-11-17 check cited -- **zero drift** since
then (`git log <old-sha>..HEAD -- <files> ` is empty). Re-check this
section (and bump the commit above) whenever hermes-agent's checkout
moves, since Spec 5's whole capture design depends on it.

This section exists because T5.1 (work-memory tool-digest capture) reads
Hermes's `messages` argument to `sync_turn`, and needs its own, narrower
contract separate from the general identity-system notes already in the
dev-extraction-spec. If a future Hermes change breaks one of the three
facts below, **the test fixture in
`tests/fixtures/hermes_messages_contract.py` must fail**, not the
work-memory code silently mis-capturing or mis-deduplicating events.

## 1. Does each tool result carry a stable `tool_call_uid`?

**Usually, but not always by the time `sync_turn` sees it.** Two
independent identity layers exist:

- **Provider-visible pairing id** (`tool_call_id` on a `role: "tool"`
  message, `call_id`/`id` on an assistant `tool_calls[i]` entry) --
  provider-controlled, can repeat, can collide, is NOT a durable
  identity by itself.
- **Hermes-minted occurrence uid** (`tool_call_uid` / `tool_call_uids`,
  `agent/message_metadata.py:92-94,228-267`) -- `uuid4().hex`
  (`mint_uid()`), never derived from content, assigned once per distinct
  provider-id **within one assistant response's tool_calls list**
  (`hermes_state_messages.py:304-326`). Stored on the assistant row as
  `tool_call_uids: {provider_id: uid, ...}` and propagated onto the
  paired tool-result row as the scalar `tool_call_uid`.

Pairing (assistant tool_calls -> tool result) happens through a
**batch_index that resets on every `role: "user"` message**
(`hermes_state_messages.py:320-321`, "a result never pairs across a
user turn"). Within one batch, the result gets `tool_call_uid` set
immediately (`:322-325`). **Across a flush/batch boundary** (the
assistant row landed in an earlier persisted batch), the uid is instead
resolved later by scanning backwards through `messages` for the nearest
preceding assistant row that named the same `tool_call_id`
(`tool_call_uid_from_history`, `message_metadata.py:270-288`), or on
restore from the DB row (`hermes_state_identity.py:77-89`). **There is
no guarantee that resolution has already run by the time `sync_turn`'s
live `messages` snapshot is taken** -- a tool-result dict without a
`tool_call_uid` key, or with it `None`, is a real, expected state, not
a bug.

**Consequence for T5.1:** hybrid-age must do its own nearest-preceding-
assistant-row resolution over the `messages` list it receives (mirroring
`tool_call_uid_from_history`'s logic), not just read `msg.get("tool_call_uid")`
and give up. If no stable key can be established even after that walk,
T5.1 already specifies: skip the event, count it, never invent an
identity.

## 2. What happens when a provider reuses or collides ids?

- A provider may legitimately **reuse one id across a batch** (e.g.
  every call named `call_0`), and some reuse the same literal id
  **turn after turn**.
- `uniquify_tool_call_ids` (`agent/message_sanitization.py:542-...`)
  renames a **new** colliding call deterministically as `<id>_d<n>`
  (never `uuid4` -- this needs to stay stable for prompt-cache
  prefixes). An id **already in session history is never rewritten**.
  So once a provider id has been assigned/seen, it is stable for the
  rest of that session; only a genuinely new collision gets suffixed.
- Composite ids from bridged transports (`"call_x|fc_y"`) are split on
  `|` by `coalesce_tool_call_id` -- only the `call_x` half is the
  pairing key (`agent/message_sanitization.py:531-539`).
- **The real identity hazard `mint_uid()` does NOT solve:** if a single
  assistant response's `tool_calls` list contains **two entries with the
  literally identical provider id** (same batch, same response -- a
  genuine same-call duplicate, not a cross-turn reuse), both calls share
  **one** minted uid (`hermes_state_messages.py:316`: "one per provider
  id: a repeat in one response shares it"). T5.1's "paired with an
  occurrence discriminator" rule is exactly the guard for this case --
  `tool_call_uid` alone is not a unique key for *distinct* same-batch
  duplicate calls, only for cross-turn reuse of the same literal id.

**Consequence for T5.1:** `tool_call_uid` is the right primary
`source_event_id`, but when two events in the SAME assistant response
would resolve to the same `tool_call_uid` (the literal-duplicate-id
case above), an occurrence discriminator (e.g. index within that
response's `tool_calls` list) must be appended, or the second event is
correctly skipped rather than silently deduplicated into the first
one's row.

## 3. Which fields actually arrive?

`messages` is the OpenAI-style list so far (`agent/memory_provider.py:151-157`),
passed into `memory_manager.sync_all` (`agent/memory_manager.py:441-443`)
and fanned out to every provider whose `sync_turn` signature accepts
`messages` (`:460-466`). Before fan-out, `_redact_for_provider`
(`:99-135`) walks every string **value** (recursively, through dicts,
lists, tuples) and scrubs secrets -- **it never touches dict keys or
structure**. `without_persistence_fields` (which WOULD strip these
identity fields, `agent/message_metadata.py:42-50,85-89`) is used only
on the model-API-bound path (`model_metadata.py:2469`,
`conversation_loop.py:1326`) -- **not** on the `sync_turn` egress path.
So the dicts hybrid-age receives DO still carry the identity fields when
Hermes has already set them.

**The literal dict keys carry a leading underscore** --
`TOOL_CALL_UID = "_tool_call_uid"`, `TOOL_CALL_UIDS = "_tool_call_uids"`
(`agent/message_metadata.py:40-41`). This is easy to get wrong by
assumption (the first draft of this contract's own fixture did, and the
contract test in `tests/test_hermes_messages_contract.py` caught it
immediately when run against the real functions) -- **always read these
as `msg.get("_tool_call_uid")` / `msg.get("_tool_call_uids")`, never
`"tool_call_uid"`/`"tool_call_uids"` without the underscore.**

Expected shape per role, as received by `sync_turn`:

```python
# assistant row with tool calls
{
    "role": "assistant",
    "content": "...",            # may be None/empty when it only calls tools
    "tool_calls": [
        {"id": "call_abc123", "type": "function",
         "function": {"name": "read_file", "arguments": "{...}"}},
        # or Codex-style: {"call_id": "call_abc123", ...}
        # or bridged:      {"call_id": "call_abc123|fc_xyz", ...}
    ],
    "_tool_call_uids": {"call_abc123": "f3a1...hex32"},  # present once Hermes has minted it; absent on legacy/unresolved rows
}

# tool-result row
{
    "role": "tool",
    "tool_call_id": "call_abc123",     # provider's own id -- NOT durable alone
    "name": "read_file",               # tool name, when present
    "content": "...",                  # the tool's own output (potentially large; T5.1 only persists allowlisted digests, never this verbatim)
    "_tool_call_uid": "f3a1...hex32",  # durable occurrence key -- may be absent, see §1
}

# user row
{"role": "user", "content": "..."}
```

`handle_tool_call` itself (`agent/tool_executor.py:1704-1714`, the live
dispatch path when a provider's own tool fires) is called with
`function_name`, `next_args`, `caller_gsk` -- **no `tool_call_id`**. This
confirms the dev-extraction-spec's note that there is no live tool-event
hook to rely on; `messages` inside `sync_turn`, after the turn, is the
only source T5.1 can use.
