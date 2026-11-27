"""Unit tests for in-process vs HTTP embed routing (no network / no model download)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes_memory.embed import (
    LOCAL_MODEL_ID,
    Embedder,
    get_shared_embedder,
    is_local_embed_url,
    resolve_local_model_id,
)


def test_empty_url_is_local():
    assert is_local_embed_url("")
    assert is_local_embed_url(None)
    assert is_local_embed_url("local")
    assert is_local_embed_url("fastembed")
    assert not is_local_embed_url("http://localhost:11434/v1")


def test_resolve_local_model_aliases():
    assert resolve_local_model_id("nomic-embed-text") == LOCAL_MODEL_ID
    assert resolve_local_model_id("") == LOCAL_MODEL_ID
    assert resolve_local_model_id("other/model") == "other/model"


@pytest.mark.asyncio
async def test_local_embed_applies_nomic_document_prefix():
    fake = MagicMock()
    fake.embed.return_value = iter([[0.1] * 768])

    with patch.object(Embedder, "_ensure_fastembed", return_value=fake):
        emb = Embedder(url="", model="nomic-embed-text", dim=768)
        vec = await emb.embed_text("hello world")

    assert vec is not None and len(vec) == 768
    fake.embed.assert_called_once()
    texts = list(fake.embed.call_args.args[0])
    assert texts == ["search_document: hello world"]


@pytest.mark.asyncio
async def test_local_embed_applies_nomic_query_prefix():
    fake = MagicMock()
    fake.embed.return_value = iter([[0.2] * 768])

    with patch.object(Embedder, "_ensure_fastembed", return_value=fake):
        emb = Embedder(url="", model="nomic-embed-text", dim=768)
        vec = await emb.embed_text("where is Mara?", as_query=True)

    assert vec is not None
    texts = list(fake.embed.call_args.args[0])
    assert texts == ["search_query: where is Mara?"]


@pytest.mark.asyncio
async def test_http_backend_skips_nomic_prefix_and_uses_openai_client():
    emb = Embedder(url="http://localhost:11434/v1", model="nomic-embed-text", dim=768)
    assert emb._local is False
    assert emb.client is not None

    fake_data = MagicMock()
    fake_data.embedding = [0.3] * 768
    fake_data.index = 0
    fake_resp = MagicMock()
    fake_resp.data = [fake_data]

    with patch.object(emb.client.embeddings, "create", return_value=fake_resp) as create:
        vec = await emb.embed_text("hello", as_query=True)

    assert vec is not None and len(vec) == 768
    create.assert_called_once()
    assert create.call_args.kwargs["input"] == ["hello"]  # no search_query prefix on HTTP


def test_get_shared_embedder_reuses_local_instance_for_same_key():
    """Every HybridAgeMemoryProvider.initialize() constructed its own
    Embedder(), each lazily loading a full separate copy of the fastembed
    ONNX model. Anything creating more than one provider in a process
    (multiple Hermes profiles, a bulk-ingest benchmark) pays that memory
    cost repeatedly -- confirmed as the dominant leak during the
    LongMemEval dry run (OOM-killed at the systemd cgroup level with "4G
    memory peak" after ~16-20 short-lived providers; closing the asyncpg
    pool explicitly did NOT stop the growth -- see issue #119's addendum
    comment). Local (in-process ONNX) embedders with the same (url,
    model, dim) key must be shared process-wide.
    """
    get_shared_embedder.cache_clear()
    e1 = get_shared_embedder(url="", model="nomic-embed-text", dim=768)
    e2 = get_shared_embedder(url="", model="nomic-embed-text", dim=768)
    assert e1 is e2, "same (url, model, dim) key must return the SAME Embedder instance"

    e3 = get_shared_embedder(url="local", model="nomic-embed-text", dim=768)
    assert e3 is e1, "'local'/''/'fastembed' aliases must resolve to the same cache key"

    e4 = get_shared_embedder(url="", model="some-other-model", dim=768)
    assert e4 is not e1, "a different model must get its own instance, not share"


def test_get_shared_embedder_does_not_share_http_backed_instances():
    """HTTP-backed embedders (Ollama/OpenAI-compatible) are NOT cached --
    each carries its own AsyncOpenAI client, which is bound to whichever
    asyncio loop created it; sharing one across providers running on
    different loops/threads would be a real cross-loop hazard, unlike
    the local ONNX case (ONNX Runtime sessions are documented as safe
    for concurrent calls from multiple threads).
    """
    get_shared_embedder.cache_clear()
    e1 = get_shared_embedder(url="http://localhost:11434/v1", model="nomic-embed-text", dim=768)
    e2 = get_shared_embedder(url="http://localhost:11434/v1", model="nomic-embed-text", dim=768)
    assert e1 is not e2, "HTTP-backed embedders must NOT be shared across callers"
