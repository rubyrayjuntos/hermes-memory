"""PR #117 review follow-ups for the Spec 8 memory_get/memory_search tools.

1. Pointers must be fetchable: a pointer to a memory_entry or doc_chunk
   carries its source in its id (``mem:<id>`` / ``doc:<id>``) and
   memory_get dispatches to the matching owner-scoped table, never to
   ``conversations`` by bare number.
2. memory_get returns the requested bodies, not another tier of
   summaries: no 600-char cut, no conversion of requested ids into
   pointers, and explicit truncation/pagination when the output budget
   binds. Redaction and escaping still apply.
3. memory_search preserves the stored speaker and uptake (the same
   overlays prefetch applies) instead of labelling every hit as
   unconfirmed assistant speech.
"""
import json
import re

from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider

OWNER = "agent-a"


class _Store:
    def __init__(self):
        self.conversations = {
            5: {"content": "conversation number five", "role": "user", "owner": OWNER},
            6: {"content": "assistant reply six", "role": "assistant", "owner": OWNER},
            7: {"content": "superseded assistant attempt", "role": "assistant", "owner": OWNER},
        }
        self.memory_entries = {
            5: {"content": "memory entry number five", "owner": OWNER},
            8: {"content": "someone else's memory entry", "owner": "agent-b"},
        }
        self.doc_chunks = {"abc123": {"content": "document chunk abc"}}
        self.overlays = {7: {"uptake": "repaired", "live_claims": 0, "retracted": []}}
        self.seeds: list[dict] = []

    async def vector_search(self, _literal, k, *, agent_identity=None):
        return self.seeds[:k]

    async def conversations_by_ids(self, ids, agent_identity=None):
        out = []
        for n in ids:
            row = self.conversations.get(int(n))
            if row and (agent_identity is None or row["owner"] == agent_identity):
                out.append({"id": str(n), "content": row["content"], "role": row["role"],
                            "ts": "2026-09-02T16:00:00+00:00", "session_id": "s1"})
        return out

    async def memory_entries_by_ids(self, ids, agent_identity=None):
        out = []
        for n in ids:
            row = self.memory_entries.get(int(n))
            if row and (agent_identity is None or row["owner"] == agent_identity):
                out.append({"id": str(n), "content": row["content"],
                            "created_at": "2026-09-01T10:00:00+00:00"})
        return out

    async def doc_chunks_by_ids(self, ids):
        return [
            {"id": i, "content": self.doc_chunks[i]["content"],
             "created_at": "2026-09-01T09:00:00+00:00"}
            for i in ids if i in self.doc_chunks
        ]

    async def span_overlays(self, ids):
        return {int(n): dict(self.overlays.get(int(n)) or
                             {"uptake": None, "live_claims": 0, "retracted": []})
                for n in ids}


class _Embedder:
    async def embed_text(self, _text, *, as_query: bool = False):
        return [1.0, 0.0]


def _provider(**cfg_kwargs):
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = _Store()
    provider.pool = None
    provider.embedder = _Embedder()
    provider.config = HybridAgeConfig(embed_dim=2, tiered_injection=True, **cfg_kwargs)
    provider._agent_identity = OWNER
    provider._last_recall_count = 0
    provider._recent_topics = []
    provider._recent_turns = []
    provider._loop = None
    return provider


def _get(provider, ids, **extra):
    return json.loads(provider.handle_tool_call("memory_get", {"ids": ids, **extra}))


def _seed(src, id_, content, sim=0.9):
    return {"id": str(id_), "src": src, "similarity": sim, "content": content,
            "ts": "2026-09-02T16:35:00+00:00", "session_id": None}


# -- 1. pointer ids carry their source; memory_get dispatches by source ------


def test_search_renders_source_qualified_ids_for_non_conversation_hits():
    provider = _provider()
    provider.store.seeds = [
        _seed("memory_entry", 5, "memory entry number five", 0.95),
        _seed("doc_chunk", "abc123", "document chunk abc", 0.94),
        _seed("conversation", 5, "conversation number five", 0.93),
    ]
    block = json.loads(provider.handle_tool_call("memory_search", {"query": "five", "k": 3}))["result"]
    assert 'id="mem:5"' in block
    assert 'id="doc:abc123"' in block
    assert 'id="5"' in block  # conversations keep their bare numeric id


def test_prefetch_pointer_ids_are_source_qualified():
    provider = _provider(full_span_cap=1, min_similarity=0.5)
    provider.store.seeds = [
        _seed("conversation", 5, "conversation number five", 0.95),
        _seed("memory_entry", 5, "memory entry number five", 0.94),
        _seed("doc_chunk", "abc123", "document chunk abc", 0.93),
    ]
    import asyncio
    block = asyncio.run(provider._aprefetch("five"))
    assert 'id="mem:5"' in block
    assert 'id="doc:abc123"' in block


def test_memory_get_mem_ref_fetches_the_memory_entry_not_the_same_numbered_conversation():
    out = _get(_provider(), ["mem:5"])
    assert "memory entry number five" in out["result"]
    assert "conversation number five" not in out["result"]


def test_memory_get_doc_ref_fetches_the_doc_chunk():
    out = _get(_provider(), ["doc:abc123"])
    assert "document chunk abc" in out["result"]
    assert "not_found_or_invalid" not in out


def test_memory_get_bare_number_still_means_conversation():
    out = _get(_provider(), [5])
    assert "conversation number five" in out["result"]
    assert "memory entry number five" not in out["result"]


def test_memory_get_foreign_owner_memory_entry_is_not_found():
    out = _get(_provider(), ["mem:8", "doc:nope", "mem:x", "bogus:1"])
    nf = out["not_found_or_invalid"]
    assert nf["mem:8"] == "not found"
    assert nf["doc:nope"] == "not found"
    assert nf["mem:x"] == "invalid id"
    assert nf["bogus:1"] == "invalid id"
    assert "someone else's memory entry" not in json.dumps(out)


def test_memory_get_schema_accepts_source_qualified_ids():
    schema = {s["name"]: s for s in _provider().get_tool_schemas()}["memory_get"]
    items = schema["parameters"]["properties"]["ids"]["items"]
    assert items.get("type") == "string"


# -- 2. memory_get returns full bodies, with explicit truncation ------------


def test_memory_get_returns_body_past_600_chars():
    provider = _provider()
    body = "start " + ("x" * 900) + " END-MARKER"
    provider.store.conversations[5]["content"] = body
    out = _get(provider, [5])
    assert "END-MARKER" in out["result"]
    assert "truncated" not in out


def test_memory_get_never_turns_requested_ids_into_pointers():
    provider = _provider()
    for n in range(10, 16):
        provider.store.conversations[n] = {"content": f"short memory {n}", "role": "user",
                                           "owner": OWNER}
    out = _get(provider, list(range(10, 16)))
    assert "<pointer" not in out["result"]
    for n in range(10, 16):
        assert f"short memory {n}" in out["result"]


def test_memory_get_reports_truncation_and_paginates_with_offset():
    provider = _provider()
    body = "".join(f"w{i:05d} " for i in range(4000))  # ~28k chars, over any budget
    provider.store.conversations[5]["content"] = body
    first = _get(provider, [5])
    assert first["truncated"]["5"]["total_chars"] == len(body.strip())
    nxt = first["truncated"]["5"]["next_offset"]
    assert 0 < nxt < len(body)
    assert 'truncated="true"' in first["result"]
    assert "w00000" in first["result"]
    second = _get(provider, [5], offset=nxt)
    assert body.strip()[nxt:nxt + 20].strip().split()[0] in second["result"]
    assert "w00000" not in second["result"]


def test_memory_get_omits_rather_than_silently_drops_when_budget_binds():
    provider = _provider()
    provider.store.conversations[5]["content"] = "y" * 30000
    out = _get(provider, [5, 6])
    # 5 fills the budget (truncated); 6 is reported as omitted, not lost.
    assert "5" in out["truncated"]
    assert out["omitted"] == ["6"]


def test_memory_get_still_redacts_secrets_beyond_the_old_600_char_window():
    provider = _provider()
    fake = "sk-or-v1-" + "0123456789abcdef" * 4
    provider.store.conversations[5]["content"] = ("filler " * 150) + f"key {fake} done"
    out = _get(provider, [5])
    assert fake not in json.dumps(out)
    assert "[REDACTED]" in out["result"]


def test_memory_get_redacts_before_paginating():
    provider = _provider()
    fake = "sk-or-v1-" + "0123456789abcdef" * 4
    body = ("z" * 5000) + " " + fake + " " + ("z" * 30000)
    provider.store.conversations[5]["content"] = body
    seen = ""
    offset = 0
    for _ in range(20):
        out = _get(provider, [5], offset=offset)
        provider.on_turn_start(0, "")
        seen += re.sub(r"<[^>]+>", "", out["result"])
        t = out.get("truncated", {}).get("5")
        if not t:
            break
        offset = t["next_offset"]
    assert "0123456789abcdef0123" not in seen


# -- 3. memory_search keeps real speaker + uptake -----------------------------


def test_memory_search_puts_user_turns_in_grounded_bin():
    provider = _provider()
    provider.store.seeds = [_seed("conversation", 5, "conversation number five")]
    block = json.loads(provider.handle_tool_call("memory_search", {"query": "five"}))["result"]
    grounded = block.split("<grounded>")[1].split("</grounded>")[0] if "<grounded>" in block else ""
    assert "conversation number five" in grounded
    assert 'speaker="user"' in block


def test_memory_search_repaired_assistant_attempt_renders_as_superseded():
    provider = _provider()
    provider.store.seeds = [
        _seed("conversation", 7, "superseded assistant attempt", 0.95),
        _seed("conversation", 6, "assistant reply six", 0.9),
    ]
    block = json.loads(provider.handle_tool_call("memory_search", {"query": "x"}))["result"]
    assert "<superseded>" in block
    assert "superseded assistant attempt" not in block
    assert 'speaker="assistant"' in block  # 6 still renders, unconfirmed


def test_memory_search_doc_and_memory_entry_hits_are_grounded_like_prefetch():
    provider = _provider()
    provider.store.seeds = [_seed("memory_entry", 5, "memory entry number five")]
    block = json.loads(provider.handle_tool_call("memory_search", {"query": "x"}))["result"]
    assert "<grounded>" in block
    assert 'speaker="doc"' in block
