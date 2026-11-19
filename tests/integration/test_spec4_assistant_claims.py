"""Spec 4 end-to-end: the key failing-test scenarios from the spec, driven
through the real provider + extraction worker against a throwaway DB, with
a deterministic fake model (no live LLM needed).

Uses the same minimal-provider-instance pattern as
test_turn_write_contract.py: items go straight through
provider._awrite_item() in the test's own event loop, never through
provider.initialize()'s background thread (that thread owns its own
event loop and asyncpg pool -- mixing it with the test's loop/pool
causes cross-loop connection errors)."""
from __future__ import annotations

import asyncio

import pytest

from hermes_memory import extraction_worker
from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider

pytestmark = [pytest.mark.store, pytest.mark.integration]

OWNER = "spec4-e2e"


class _FakeEmbedder:
    async def embed_text(self, text: str, *, as_query: bool = False):
        vec = [0.0] * 768
        vec[0] = 0.5
        return vec


def _make_provider(store, embedder, *, extract_assistant_replies: bool) -> HybridAgeMemoryProvider:
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = HybridAgeConfig(
        dsn="unused",
        extract_mode="rules_then_model",
        extract_url="http://fake-model/v1",
        extract_model="fake-model",
        extract_assistant_replies=extract_assistant_replies,
    )
    p.pool = store.pool
    p.store = store
    p.embedder = embedder
    p._agent_identity = OWNER
    p._session_id = "default"
    p._last_turn_id = {}
    p._write_queue = None
    p._loop = None
    p._primary_context = True
    p._recent_topics = []
    p._recent_turns = []
    p._max_topics = 8
    p._max_turns = 6
    return p


async def _write_turn_pair(provider, session_id: str, user_content: str,
                           assistant_content: str) -> tuple[int, int]:
    """Writes a user turn then its paired assistant reply, resolving
    previous_conversation_id the same way sync_turn's drain does."""
    await provider._awrite_item({
        "type": "turn", "session_id": session_id, "agent_identity": OWNER,
        "role": "user", "content": user_content, "previous_conversation_id": None,
    })
    async with provider.store.pool.acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM conversations WHERE agent_identity = $1 AND session_id = $2 "
            "ORDER BY id DESC LIMIT 1",
            OWNER, session_id,
        )
    await provider._awrite_item({
        "type": "turn", "session_id": session_id, "agent_identity": OWNER,
        "role": "assistant", "content": assistant_content,
        "previous_conversation_id": int(user_id) if user_id is not None else None,
    })
    async with provider.store.pool.acquire() as conn:
        assistant_id = await conn.fetchval(
            "SELECT id FROM conversations WHERE agent_identity = $1 AND session_id = $2 "
            "ORDER BY id DESC LIMIT 1",
            OWNER, session_id,
        )
    return int(user_id), int(assistant_id)


async def _run_model_jobs_until_idle(provider, fake_model, session_id: str = "default",
                                     max_iters: int = 30) -> None:
    await provider.store.job_flush_model_batches(OWNER, session_id)
    for _ in range(max_iters):
        finished = await extraction_worker.run_extraction_once(
            provider, provider.store,
            embed=lambda texts: asyncio.gather(*[_FakeEmbedder().embed_text(t) for t in texts]),
            model=fake_model,
        )
        async with provider.store.pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT count(*) FROM durable_job WHERE owner = $1 "
                "AND kind IN ('extract_rules', 'extract_model') "
                "AND status IN ('pending', 'leased', 'retryable')",
                OWNER,
            )
        if pending == 0:
            return
        if finished == 0:
            await asyncio.sleep(0.05)


def _fake_model_for(mapping: dict[str, list[dict]]):
    """mapping: substring of turn text -> claim templates. Each template's
    "subject" is only used verbatim for assistant turns (where the model
    genuinely says "you..."); for a user turn, the subject is remapped to
    "I" to match how the user actually phrases a first-person statement --
    using the literal template subject against a user turn would wrongly
    expect the literal word "you" to appear in the user's own words."""
    async def model(batch):
        claims = []
        for turn in batch.turns:
            for needle, template_claims in mapping.items():
                if needle in turn["text"]:
                    for tc in template_claims:
                        claim = {"span_id": turn["span_id"], **tc}
                        if turn.get("speaker") == "user" and claim.get("subject") == "you":
                            claim["subject"] = "I"
                        claims.append(claim)
        return {"claims": claims}
    return model


@pytest.mark.asyncio
async def test_echo_backs_up_user_fact_claim_count_stays_one(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), extract_assistant_replies=True)
    await _write_turn_pair(
        provider, "s1", "I live in NYC.",
        "Thanks for sharing that -- noted, you live in NYC these days.",
    )

    fake_model = _fake_model_for({
        "you live in NYC these days": [
            {"subject": "you", "verb": "lives_in", "object": "NYC", "polarity": "positive"},
        ],
    })
    await _run_model_jobs_until_idle(provider, fake_model, session_id="s1")

    async with store.pool.acquire() as conn:
        claim_rows = await conn.fetch(
            "SELECT claim_id FROM claims WHERE subject = '(self)' AND verb = 'lives_in' "
            "AND object = 'NYC'",
        )
        claim_ids = sorted({r["claim_id"] for r in claim_rows})
        evidence_rows = await conn.fetch(
            "SELECT DISTINCT speaker FROM claim_evidence WHERE claim_id = ANY($1::bigint[])",
            claim_ids,
        ) if claim_ids else []

    assert len(claim_ids) == 1, f"expected exactly 1 merged claim, got {len(claim_ids)}"
    speakers = sorted(r["speaker"] for r in evidence_rows)
    assert "user" in speakers and "assistant" in speakers, (
        f"expected both user and assistant evidence on the merged claim, got {speakers}"
    )


@pytest.mark.asyncio
async def test_novel_assistant_fact_is_unconfirmed_and_never_current(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), extract_assistant_replies=True)
    await _write_turn_pair(
        provider, "s2", "How should I plan my week?",
        "You prefer working from home on Fridays, and I will remember that.",
    )

    fake_model = _fake_model_for({
        "working from home on Fridays": [
            {"subject": "you", "verb": "prefers", "object": "working from home on Fridays",
             "polarity": "positive"},
        ],
    })
    await _run_model_jobs_until_idle(provider, fake_model, session_id="s2")

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, valid_to FROM claims WHERE subject = '(self)' AND verb = 'prefers' "
            "AND object = 'working from home on Fridays'",
        )
        grounded_count = await conn.fetchval(
            "SELECT count(*) FROM claims WHERE subject = '(self)' AND verb = 'prefers' "
            "AND object = 'working from home on Fridays' "
            "AND status = 'grounded' AND valid_to IS NULL",
        )

    assert len(rows) >= 1, "expected the novel assistant fact to create a claim row"
    for row in rows:
        assert row["status"] == "unconfirmed", (
            f"assistant-only fact must stay unconfirmed, got status={row['status']!r}"
        )
    assert grounded_count == 0, "assistant-only fact must never surface as a grounded/current claim"


@pytest.mark.asyncio
async def test_reply_with_no_fact_creates_no_claim(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), extract_assistant_replies=True)
    await _write_turn_pair(
        provider, "s3", "Can you summarize the plan for the next sprint please?",
        "Sure, I will put together a short summary of the sprint plan for you shortly.",
    )

    fake_model = _fake_model_for({})
    await _run_model_jobs_until_idle(provider, fake_model, session_id="s3")

    async with store.pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM claims")
    assert count == 0, "a reply with no fact must create zero claims"


@pytest.mark.asyncio
async def test_memory_block_quoted_reply_produces_no_claims(store, clean_hermes_test_db):
    provider = _make_provider(store, _FakeEmbedder(), extract_assistant_replies=True)
    quoting_reply = (
        "Here's what I have stored about you so far, for reference:\n"
        "<memory>\n  <grounded>\n    you lives_in Reykjavik [positive]\n  </grounded>\n</memory>\n"
        "Let me know if anything there looks wrong."
    )
    await _write_turn_pair(provider, "s4", "What do you remember about me?", quoting_reply)

    fake_model = _fake_model_for({
        "Reykjavik": [
            {"subject": "you", "verb": "lives_in", "object": "Reykjavik", "polarity": "positive"},
        ],
    })
    await _run_model_jobs_until_idle(provider, fake_model, session_id="s4")

    async with store.pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM claims WHERE object = 'Reykjavik'")
    assert count == 0, (
        "the quoted <memory> block's content must never reach extraction "
        "(stripper must run before the model ever sees the text)"
    )


@pytest.mark.asyncio
async def test_assistant_evidence_never_promotes_to_current_only_user_turn_does(
    store, clean_hermes_test_db,
):
    provider = _make_provider(store, _FakeEmbedder(), extract_assistant_replies=True)
    fake_model = _fake_model_for({
        "working from home on Fridays": [
            {"subject": "you", "verb": "prefers", "object": "working from home on Fridays",
             "polarity": "positive"},
        ],
    })

    for i in range(3):
        await _write_turn_pair(
            provider, "s5", f"Anything else to note? (round {i})",
            "You prefer working from home on Fridays, just confirming again.",
        )
        await _run_model_jobs_until_idle(provider, fake_model, session_id="s5")

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status FROM claims WHERE subject = '(self)' AND verb = 'prefers' "
            "AND object = 'working from home on Fridays'",
        )
    assert rows, "expected at least one claim row from the repeated assistant fact"
    for row in rows:
        assert row["status"] != "grounded", (
            "repeated assistant-only evidence must never promote the claim to grounded/current"
        )

    # Now the user confirms the same fact directly -- only this should promote it.
    await provider._awrite_item({
        "type": "turn", "session_id": "s5", "agent_identity": OWNER,
        "role": "user", "content": "Yes, I prefer working from home on Fridays.",
        "previous_conversation_id": None,
    })
    await _run_model_jobs_until_idle(provider, fake_model, session_id="s5")

    async with store.pool.acquire() as conn:
        grounded_count = await conn.fetchval(
            "SELECT count(*) FROM claims WHERE subject = '(self)' AND verb = 'prefers' "
            "AND object = 'working from home on Fridays' "
            "AND status = 'grounded' AND valid_to IS NULL",
        )
    assert grounded_count >= 1, "a user turn confirming the fact must promote it to current"
