"""hermes_memory.embed — embedding client with the 768-dim invariant.

Default backend is in-process ``fastembed`` (ONNX ``nomic-ai/nomic-embed-text-v1.5``,
768-d). No Ollama daemon is required. Set ``HYBRID_AGE_EMBED_URL`` to an
OpenAI-compatible HTTP endpoint (Ollama, OpenAI, etc.) to override.

``embed_text()`` / ``embed_texts()`` return vectors of exactly the configured
dimension (default 768) or ``None``. They NEVER return a wrong-dimension
vector — callers can rely on ``vec is not None`` implying dimensionality.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional, Sequence

logger = logging.getLogger("hybrid_age.embed")

# Stamp written to embed_model columns / trust_nomic_768 policy.
STAMP_MODEL = "nomic-embed-text"
# fastembed / Hugging Face id for the in-process ONNX model (768-d).
LOCAL_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
_LOCAL_ALIASES = {
    "",
    "nomic-embed-text",
    "nomic-embed-text:latest",
    "nomic-embed-text-v1.5",
    LOCAL_MODEL_ID,
}


def is_local_embed_url(url: Optional[str]) -> bool:
    """True when embeddings should run in-process (no HTTP client)."""
    u = (url or "").strip().lower()
    return u in ("", "local", "inprocess", "fastembed", "-")


def resolve_local_model_id(model: str) -> str:
    """Map config / Ollama-style names onto the fastembed model id."""
    key = (model or "").strip()
    if key.lower() in {a.lower() for a in _LOCAL_ALIASES} or not key:
        return LOCAL_MODEL_ID
    return key


class Embedder:
    """768-d embedder: in-process fastembed by default, optional OpenAI-compatible HTTP."""

    def __init__(self, url: Optional[str], model: str, dim: int = 768):
        self.url = (url or "").strip()
        self.model = model or STAMP_MODEL
        self.dim = dim
        self._local = is_local_embed_url(self.url)
        self._fe = None  # lazy TextEmbedding
        self.client = None
        if not self._local:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(
                base_url=self.url,
                api_key=os.environ.get("HYBRID_AGE_EMBED_KEY", "ollama"),
            )

    def _nomic_prefix(self, text: str, *, as_query: bool) -> str:
        """nomic-embed-text-v1.5 requires search_query / search_document prefixes."""
        if self._local and resolve_local_model_id(self.model) == LOCAL_MODEL_ID:
            kind = "search_query: " if as_query else "search_document: "
            if text.startswith("search_query:") or text.startswith("search_document:"):
                return text
            return kind + text
        return text

    def _ensure_fastembed(self):
        if self._fe is not None:
            return self._fe
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "in-process embeddings require the 'fastembed' package "
                "(pip install hermes-memory / fastembed)"
            ) from exc
        model_id = resolve_local_model_id(self.model)
        logger.info("loading in-process embedder model=%s", model_id)
        self._fe = TextEmbedding(model_name=model_id)
        return self._fe

    def _embed_local_sync(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        model = self._ensure_fastembed()
        # fastembed yields one vector per input in order.
        vectors = list(model.embed(list(texts)))
        out: List[Optional[List[float]]] = []
        for vec in vectors:
            arr = list(vec)
            if len(arr) != self.dim:
                logger.warning(
                    "embedding dim mismatch: got %s expected %s — dropping",
                    len(arr), self.dim,
                )
                out.append(None)
            else:
                out.append(arr)
        return out

    async def embed_text(self, text: str, *, as_query: bool = False) -> Optional[List[float]]:
        """Embed text; return None on any failure or dimension mismatch."""
        if not text or not text.strip():
            return None
        results = await self.embed_texts([text], as_query=as_query)
        return results[0] if results else None

    async def embed_texts(
        self, texts: Sequence[str], *, as_query: bool = False
    ) -> List[Optional[List[float]]]:
        """Batch embed; one result per input (None on empty/failure/dim mismatch)."""
        if not texts:
            return []
        out: List[Optional[List[float]]] = [None] * len(texts)
        payload: List[str] = []
        index_map: List[int] = []
        for i, text in enumerate(texts):
            if text and str(text).strip():
                payload.append(self._nomic_prefix(str(text), as_query=as_query))
                index_map.append(i)
        if not payload:
            return out

        if self._local:
            try:
                vectors = await asyncio.to_thread(self._embed_local_sync, payload)
            except Exception:
                logger.warning("local embed failed", exc_info=True)
                return out
            for orig_i, vec in zip(index_map, vectors):
                out[orig_i] = vec
            return out

        assert self.client is not None
        try:
            resp = await self.client.embeddings.create(model=self.model, input=payload)
        except Exception:
            logger.debug("embed batch failed; falling back per item", exc_info=True)
            for j, orig_i in enumerate(index_map):
                try:
                    resp_one = await self.client.embeddings.create(
                        model=self.model, input=payload[j]
                    )
                    vec = list(resp_one.data[0].embedding)
                except Exception:
                    logger.warning("embed failed", exc_info=True)
                    continue
                if len(vec) != self.dim:
                    logger.warning(
                        "embedding dim mismatch: got %s expected %s — dropping",
                        len(vec), self.dim,
                    )
                    continue
                out[orig_i] = vec
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


def warm_local_embedder(model: str = STAMP_MODEL, dim: int = 768) -> None:
    """Download/cache the ONNX model (install-time / first-run warm)."""
    emb = Embedder(url="", model=model, dim=dim)
    emb._ensure_fastembed()
    # One short embed forces weights into memory and validates dim.
    vecs = emb._embed_local_sync([emb._nomic_prefix("warmup", as_query=False)])
    if not vecs or vecs[0] is None:
        raise RuntimeError("in-process embedder warm-up produced no vector")


_shared_embedder_cache: dict[tuple[str, str, int], "Embedder"] = {}


def get_shared_embedder(url: Optional[str], model: str, dim: int = 768) -> "Embedder":
    """Process-wide Embedder cache, shared ONLY for local (in-process
    fastembed/ONNX) configs -- HTTP-backed configs always get a fresh
    instance (see below for why).

    Every HybridAgeMemoryProvider.initialize() previously constructed
    its own Embedder(), each lazily loading a full separate copy of the
    fastembed ONNX model (nomic-embed-text-v1.5). Any process creating
    more than one provider -- several Hermes profiles sharing a process,
    a bulk-ingest benchmark -- paid that memory cost once per provider.
    Confirmed as the dominant real-world leak during the LongMemEval
    dry run: explicitly closing the asyncpg connection pool did NOT stop
    memory growth; only sharing the embedder did (see issue #119's
    addendum comment for the full trace, including a systemd-cgroup OOM
    kill with "4G memory peak" after ~16-20 short-lived providers).

    Local embedders are safe to share: ONNX Runtime sessions are
    documented as safe for concurrent Run() calls from multiple threads,
    and fastembed's TextEmbedding wraps exactly that. HTTP-backed
    embedders are deliberately NOT cached here -- each one carries its
    own AsyncOpenAI client, which binds to whichever asyncio loop
    constructed it; sharing one across providers running on different
    event loops/threads (which is exactly how HybridAgeMemoryProvider
    is structured -- one dedicated loop thread per provider) would be a
    real cross-loop hazard, not a safe optimization.
    """
    if not is_local_embed_url(url):
        return Embedder(url=url, model=model, dim=dim)
    key = (resolve_local_model_id(model), "", dim)
    cached = _shared_embedder_cache.get(key)
    if cached is None:
        cached = Embedder(url=url, model=model, dim=dim)
        _shared_embedder_cache[key] = cached
    return cached


def _clear_shared_embedder_cache() -> None:
    """Test-only: reset the process-wide cache between test cases."""
    _shared_embedder_cache.clear()


get_shared_embedder.cache_clear = _clear_shared_embedder_cache  # type: ignore[attr-defined]


def vec_to_literal(vec: List[float]) -> str:
    """Convert a float list to a pgvector literal ``'[0.1,0.2,...]'``."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"
