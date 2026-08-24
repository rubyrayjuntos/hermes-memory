"""
hermes_memory.embed — embedding abstraction layer.

Supports multiple providers:
- ollama (local, default)
- openai
- voyage
- cohere

Provider selection is driven by config.yaml or HYBRID_AGE_EMBED_PROVIDER env var.
"""
from __future__ import annotations

from typing import List

from openai import AsyncOpenAI


class EmbeddingBackend:
    """Abstract embedding interface."""

    async def embed(self, text: str) -> List[float]:
        raise NotImplementedError


class OllamaEmbedding(EmbeddingBackend):
    def __init__(self, url: str = "http://localhost:11434/v1", model: str = "nomic-embed-text"):
        self.url = url
        self.model = model
        self.client = AsyncOpenAI(base_url=url, api_key="ollama")

    async def embed(self, text: str) -> List[float]:
        resp = await self.client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding


class OpenAIEmbedding(EmbeddingBackend):
    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    async def embed(self, text: str) -> List[float]:
        resp = await self.client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding


def get_embedding_backend() -> EmbeddingBackend:
    """Factory: return the configured embedding backend."""
    import os
    provider = os.environ.get("HYBRID_AGE_EMBED_PROVIDER", "ollama").lower()
    if provider == "openai":
        return OpenAIEmbedding(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("HYBRID_AGE_EMBED_MODEL", "text-embedding-3-small"),
        )
    # Default: ollama
    return OllamaEmbedding(
        url=os.environ.get("HYBRID_AGE_EMBED_URL", "http://localhost:11434/v1"),
        model=os.environ.get("HYBRID_AGE_EMBED_MODEL", "nomic-embed-text"),
    )
