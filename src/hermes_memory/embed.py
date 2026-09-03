"""hermes_memory.embed — embedding client with the 768-dim invariant.

``embed_text()`` returns a list of floats of exactly the configured dimension
(default 768, matching ``vector(768)`` columns) or ``None``. It NEVER returns
a wrong-dimension vector — callers can rely on ``vec is not None`` implying
dimensionality.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Sequence

from openai import AsyncOpenAI

logger = logging.getLogger("hybrid_age.embed")


class Embedder:
    """OpenAI-compatible embedding client (Ollama serves /v1 natively)."""

    def __init__(self, url: str, model: str, dim: int = 768):
        self.url = url
        self.model = model
        self.dim = dim
        self.client = AsyncOpenAI(base_url=url, api_key=os.environ.get("HYBRID_AGE_EMBED_KEY", "ollama"))

    async def embed_text(self, text: str) -> Optional[List[float]]:
        """Embed text; return None on any failure or dimension mismatch."""
        if not text or not text.strip():
            return None
        try:
            resp = await self.client.embeddings.create(model=self.model, input=text)
            vec = resp.data[0].embedding
        except Exception:
            logger.debug("embed failed", exc_info=True)
            return None
        if not vec or len(vec) != self.dim:
            logger.warning(
                "embedding dim mismatch: got %s expected %s — dropping",
                len(vec) if vec else 0, self.dim,
            )
            return None
        return list(vec)

    async def embed_texts(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        """Batch embed; one result per input (None on empty/failure/dim mismatch)."""
        if not texts:
            return []
        out: List[Optional[List[float]]] = [None] * len(texts)
        payload: List[str] = []
        index_map: List[int] = []
        for i, text in enumerate(texts):
            if text and str(text).strip():
                payload.append(str(text))
                index_map.append(i)
        if not payload:
            return out
        try:
            resp = await self.client.embeddings.create(model=self.model, input=payload)
        except Exception:
            logger.debug("embed batch failed; falling back per item", exc_info=True)
            for orig_i in index_map:
                out[orig_i] = await self.embed_text(str(texts[orig_i]))
            return out
        data = list(resp.data)
        if len(data) == len(payload):
            pairs = list(zip(index_map, data))
        else:
            pairs = []
            for d in data:
                try:
                    j = int(d.index)
                except Exception:
                    continue
                if 0 <= j < len(index_map):
                    pairs.append((index_map[j], d))
        for orig_i, d in pairs:
            vec = list(d.embedding) if getattr(d, "embedding", None) else None
            if not vec or len(vec) != self.dim:
                if vec:
                    logger.warning(
                        "embedding dim mismatch: got %s expected %s — dropping",
                        len(vec), self.dim,
                    )
                continue
            out[orig_i] = vec
        return out


def vec_to_literal(vec: List[float]) -> str:
    """Convert a float list to a pgvector literal ``'[0.1,0.2,...]'``."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"
