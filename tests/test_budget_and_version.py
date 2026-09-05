"""Budget, HNSW query param, and embedding version helpers."""
from __future__ import annotations

import inspect
import logging

import pytest

from hermes_memory.provider import (
    HybridAgeMemoryProvider,
    _is_missing_embed_version_schema,
)
from hermes_memory.store import Store, clamp_hnsw_ef_search, embedding_dim_of, hnsw_ef_search_sql
from hermes_memory.tokens import count_tokens, injection_token_cap


GOSP_DENSE = (
    "MENTIONS EMA e_tgt_vec passport provenance_turns chunk_id conv_41 noun_id "
    "MERGE SET LOCAL hnsw.ef_search beam_score src_align tgt_align "
    "walk_hypothesis composite * (magnitude / 8) RateLimiter nomic-embed-text"
)
CODE_DENSE = "def foo(x):\n    return x + 1  # noqa: AGE MERGE {name: 'Turn'}\n" * 30


def test_count_tokens_exceeds_char_div_4_on_code_like_text() -> None:
    blob = "def foo(x):\n    return x\n" * 40
    assert count_tokens(blob) > len(blob) // 4


def test_budget_seeds_uses_tokens_not_char_div_4() -> None:
    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)

    class Cfg:
        max_tokens = 80

    provider.config = Cfg()
    fat = "αβγδεζηθικλμνξοπ" * 20
    assert count_tokens(fat) > 80
    kept = provider._budget_seeds([
        {"content": fat, "score": 0.9, "turn_id": 1},
        {"content": "short hit", "score": 0.8, "turn_id": 2},
    ])
    ids = [s["turn_id"] for s in kept]
    assert 1 not in ids
    assert 2 in ids


def test_hnsw_ef_search_sql_is_bounded_int() -> None:
    assert hnsw_ef_search_sql(100) == "SET LOCAL hnsw.ef_search = 100"
    assert hnsw_ef_search_sql(9999) == "SET LOCAL hnsw.ef_search = 400"
    assert hnsw_ef_search_sql(1) == "SET LOCAL hnsw.ef_search = 10"
    assert clamp_hnsw_ef_search(7) == 10
    assert clamp_hnsw_ef_search(401) == 400


def test_insert_sql_names_embed_version_columns() -> None:
    """Pre-V10 schema cannot silently skip the stamp: INSERT lists the columns."""
    src = inspect.getsource(Store.insert_turn)
    assert "embed_model" in src and "embed_dim" in src
    upsert = inspect.getsource(Store.upsert_memory_entry)
    assert "embed_model" in upsert and "embed_dim" in upsert


def test_missing_embed_columns_are_schema_failures() -> None:
    class UndefinedColumnError(Exception):
        pass

    assert _is_missing_embed_version_schema(
        Exception('column "embed_model" of relation "conversations" does not exist')
    )
    assert _is_missing_embed_version_schema(UndefinedColumnError("nope"))
    assert not _is_missing_embed_version_schema(RuntimeError("connection reset"))


@pytest.mark.asyncio
async def test_missing_embed_columns_log_error_not_debug(caplog) -> None:
    class BoomStore:
        async def insert_turn(self, *args, **kwargs):
            raise Exception('column "embed_model" of relation "conversations" does not exist')

    class Emb:
        async def embed_text(self, text):
            return [0.0] * 768

    provider = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    provider._agent_identity = "default"
    provider._last_turn_id = {}
    with caplog.at_level(logging.ERROR, logger="hybrid_age"):
        await provider._awrite_turn(
            BoomStore(),
            Emb(),
            {"session_id": "s", "role": "user", "content": "hello there memory"},
        )
    assert "apply V10" in caplog.text
    assert "embed_model" in caplog.text


def test_injection_token_cap_leaves_slack_for_non_openai_consumers() -> None:
    assert injection_token_cap(1200) == 1080
    assert injection_token_cap(80) == 72


def test_cl100k_vs_o200k_margin_on_gosp_and_code() -> None:
    """Injection is read by Nemotron/Grok/Claude/GPT — cl100k is an approximation.

    Measure o200k (GPT-4o family) vs cl100k on the same GOSP/code blobs. A 10%
    pack slack covers the observed ratio; Claude is not locally tokenizable here.
    """
    try:
        import tiktoken
    except ImportError:
        pytest.skip("tiktoken not installed")
    cl100k = tiktoken.get_encoding("cl100k_base")
    try:
        o200k = tiktoken.get_encoding("o200k_base")
    except Exception:
        pytest.skip("o200k_base not in this tiktoken")
    for blob in (GOSP_DENSE, CODE_DENSE):
        a = len(cl100k.encode(blob))
        b = len(o200k.encode(blob))
        ratio = b / a if a else 1.0
        assert 0.75 <= ratio <= 1.25, f"tokenizer ratio {ratio:.3f} on {blob[:40]!r}"
        over = max(0.0, (b - a) / a) if a else 0.0
        # Pack slack is 10%; fail if o200k needs more than that on these blobs.
        assert over <= 0.10 + 1e-9, f"o200k overcount {over:.3f} exceeds 10% slack"


def test_wrong_embedding_dim_is_rejected() -> None:
    from hermes_memory.store import assert_embedding_compatible

    assert_embedding_compatible("[1,0,0]", 3)
    try:
        assert_embedding_compatible("[1,0]", 768)
    except ValueError as exc:
        assert "dim 2 != 768" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert embedding_dim_of("[1,0,0]") == 3
    assert embedding_dim_of(None) is None


def test_recall_frozen_turn_ids_survive_injection() -> None:
    from hermes_memory.provider_helpers import format_injection

    block = format_injection([
        {
            "score": 0.91,
            "content": "turn 41: RateLimiter talks to nomic-embed-text",
            "turn_id": 41,
            "session_id": "sess-a",
            "created_at": "2026-09-02T16:35:00+00:00",
        },
        {
            "score": 0.88,
            "content": "turn 42: second keep",
            "turn_id": 42,
            "session_id": "sess-a",
            "created_at": "2026-09-02T16:35:30+00:00",
        },
        {
            "score": 0.4,
            "content": "turn 99: unrelated",
            "turn_id": 99,
            "session_id": "sess-a",
            "created_at": "2026-09-02T16:36:00+00:00",
        },
    ])
    assert "turn 41: RateLimiter talks to nomic-embed-text" in block
    assert "turn 99: unrelated" not in block
