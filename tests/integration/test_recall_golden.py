"""Frozen recall gate: the prompt contract an ABOUT-linker PR cannot break.

Seeds controlled turns into hermes_test (isolated; never prod), runs the live
_prefetch with the ANN door only (the graph walk is monkeypatched out — that
is the contract direction, not a shortcut), and checks the frozen JSON cases:
expected spans present, triple DSL absent, silence on impossible thresholds.

Relevance ranking quality is NOT gated here — that gold awaits labeled uptake
pairs. This file locks the envelope: bins, silence, no verbalized walks.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_memory.config import HybridAgeConfig
from hermes_memory.embed import vec_to_literal
from hermes_memory.provider import HybridAgeMemoryProvider

pytestmark = [pytest.mark.store, pytest.mark.integration]

GOLDEN = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "recall-golden.json").read_text()
)


class FakeEmbedder:
    async def embed_text(self, text: str):
        h = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [0.0] * 768
        vec[0] = ((h[0] % 97) / 97.0) or 0.01
        vec[1] = ((h[1] % 97) / 97.0) or 0.02
        vec[2] = ((h[2] % 97) / 97.0) or 0.03
        vec[3] = 0.04
        return vec


def _make_provider(store, min_similarity: float) -> HybridAgeMemoryProvider:
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.config = HybridAgeConfig(min_similarity=min_similarity)
    p.pool = store.pool
    p.store = store
    p.embedder = FakeEmbedder()
    p._agent_identity = "default"
    p._session_id = "default"
    p._last_turn_id = {}
    p._write_queue = None
    p._loop = None
    p._primary_context = True
    p._recent_topics = []
    p._recent_turns = []
    p._max_topics = 8
    p._max_turns = 6
    p._last_recall_count = 0
    return p


async def _seed(store) -> None:
    # hermes_test persists across runs: reseed idempotently, not additively.
    async with store.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM conversations WHERE session_id LIKE 'golden-%%'")
    emb = FakeEmbedder()
    for session_id, turns in GOLDEN["seed"]["sessions"].items():
        for t in turns:
            vec = await emb.embed_text(t["content"])
            await store.insert_turn(
                session_id, "default", t["role"], t["content"],
                vec_to_literal(vec), metadata={"kind": "interactive"},
            )


@pytest.mark.asyncio
async def test_recall_golden_contract(store) -> None:
    await _seed(store)
    provider = _make_provider(store, 0.0)

    async def _no_walk(*a, **k):
        return []

    provider._expand_paths = _no_walk  # type: ignore[method-assign]
    for case in GOLDEN["cases"]:
        provider.config = HybridAgeConfig(
            min_similarity=case.get("min_similarity", 0.0))
        block = await provider._aprefetch(case["query"])
        if case.get("expect_empty"):
            assert block.strip() == "", f"expected silence for {case['query']!r}"
            continue
        for needle in case.get("expect_present", []):
            assert needle in block, f"{needle!r} missing for {case['query']!r}"
        for needle in case.get("expect_absent", []):
            assert needle not in block, f"{needle!r} leaked for {case['query']!r}"
