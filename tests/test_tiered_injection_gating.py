"""Spec 8 gating: tiered_injection defaults to False, and when it is False
_aprefetch's real call site must behave EXACTLY as it did before Spec 8 --
no count cap on full spans, no pointer tier -- even though
format_span_injection's own defaults (full_span_cap=4 etc.) are now always
tiered when called directly. The provider's call site must pass explicit
"no cap" values when the flag is off, so Spec 8 ships genuinely dormant.
"""
from hermes_memory.config import HybridAgeConfig
from hermes_memory.provider import HybridAgeMemoryProvider


class _Store:
    def __init__(self, n=8):
        self._n = n

    async def vector_search(self, _literal, _k, *, agent_identity=None):
        return [{
            "id": str(i), "src": "conversation", "similarity": 0.9 - i * 0.01,
            "content": f"seed turn number {i} about something related",
            "embedding": "[1,0]", "session_id": "s1",
            "ts": "2026-09-02T16:35:00+00:00",
        } for i in range(1, self._n + 1)]

    async def passports_for_conversations(self, _conv_ids):
        return []


class _Embedder:
    async def embed_text(self, _text, *, as_query: bool = False):
        return [1.0, 0.0]


def _provider(**cfg_kwargs):
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider.store = _Store()
    provider.pool = None
    provider.embedder = _Embedder()
    provider.config = HybridAgeConfig(embed_dim=2, min_similarity=0.5, **cfg_kwargs)
    provider._agent_identity = "agent-a"
    provider._last_recall_count = 0
    provider._recent_topics = []
    provider._recent_turns = []
    return provider


async def test_tiered_injection_off_by_default_preserves_old_unlimited_full_span_behavior():
    provider = _provider()  # tiered_injection defaults to False
    assert provider.config.tiered_injection is False
    block = await provider._aprefetch("something related")
    # 8 seeds, no cap: all 8 must render as full <span> elements, NOT
    # truncated to full_span_cap=4 and NOT overflowed into <pointer> --
    # this is the exact pre-Spec-8 behavior.
    full_spans = {i for i in range(1, 9) if f'<span id="{i}"' in block}
    assert full_spans == set(range(1, 9)), (
        "tiered_injection=False must not apply Spec 8's count cap at all"
    )
    assert "<pointer" not in block
    assert "<more " not in block


async def test_tiered_injection_on_applies_the_configured_tiers():
    provider = _provider(tiered_injection=True, full_span_cap=3, pointer_cap=10)
    block = await provider._aprefetch("something related")
    full_spans = {i for i in range(1, 9) if f'<span id="{i}"' in block}
    pointer_spans = {i for i in range(1, 9) if f'<pointer id="{i}"' in block}
    assert len(full_spans) == 3
    assert pointer_spans == set(range(1, 9)) - full_spans
