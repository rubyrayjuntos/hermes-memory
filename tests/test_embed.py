"""Unit tests for in-process vs HTTP embed routing (no network / no model download)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes_memory.embed import (
    LOCAL_MODEL_ID,
    Embedder,
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
