"""hermes_memory.provider — HybridAgeMemoryProvider(MemoryProvider).

Port of the production-proven plugin (~/.hermes/plugins/hybrid-age/provider.py)
to the packaged provider layout, per docs/plans/v0.1.md §3.1.

Contract highlights:
- name = "hybrid-age"
- is_available(): no network — config resolvable + driver importable
- initialize(session_id, **kwargs): skip writes unless agent_context == "primary"
- prefetch(): sync, never raises, fenced block <= 1200 tokens, <2s warm
- sync_turn(): enqueue-only; asyncio.Queue(maxsize=256) with drop counting
- on_memory_write(action, target, content, metadata=None): exact kwarg
- get_config_schema() / save_config() power `hermes memory setup`
- shutdown(): drain queue <= 5s, close pool
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # running outside the Hermes runtime (tests, CI)
    MemoryProvider = object  # type: ignore[assignment,misc]

from .config import CONFIG_SCHEMA_FIELDS, HybridAgeConfig, load_config
from .embed import Embedder, vec_to_literal
from .store import Store

logger = logging.getLogger("hybrid_age")

SECRET_RE = re.compile(r"api[_-]?key|secret|password|BEGIN PRIVATE", re.I)

TURN_MIN_CHARS = 40
_NOISE_RE = re.compile(
    r"^("
    r"ok(ay)?|thanks?( you)?|thx|ty|np|"
    r"yes|no|sure|got it|done|cool|nice|great|"
    r"continue|please|exit|cancel|stop|quit|"
    r"yeah|yep|nope|alright"
    r")[\s\.\!\?]*$",
    re.IGNORECASE,
)

SHUTDOWN_DRAIN_S = 5.0


def _is_noise(content: str, *, min_chars: int = TURN_MIN_CHARS) -> bool:
    stripped = (content or "").strip()
    if not stripped:
        return True
    if len(stripped) < min_chars:
        return True
    return bool(_NOISE_RE.match(stripped))


class HybridAgeMemoryProvider(MemoryProvider):
    def __init__(self, config: Optional[HybridAgeConfig] = None) -> None:
        self.config = config or load_config()
        self.pool = None
        self.store: Optional[Store] = None
        self.embedder: Optional[Embedder] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session_id = ""
        self._agent_identity = ""
        self._write_queue: Optional[asyncio.Queue] = None
        self._dropped_writes = 0
        self._primary_context = True  # writes allowed until told otherwise
        # Session context — rolling window of recent topics/turns for query enrichment
        self._recent_topics: List[str] = []
        self._recent_turns: List[str] = []
        self._max_topics = 8
        self._max_turns = 6
        self._last_recall_count = 0
        self._unavailable_reason = ""

    # -- identity -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "hybrid-age"

    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def is_available(self) -> bool:
        """No network calls: config resolvable + drivers importable."""
        try:
            import asyncpg  # noqa: F401
            from openai import AsyncOpenAI  # noqa: F401
        except ImportError as exc:
            self._unavailable_reason = f"missing dependency: {exc.name}; pip install hermes-memory"
            return False
        cfg = self.config or load_config()
        if not cfg.dsn:
            self._unavailable_reason = (
                f"no DSN: set {cfg.dsn_env} in the environment or ~/.hermes/.env"
            )
            return False
        self._unavailable_reason = ""
        return True

    # -- lifecycle --------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.config = self.config or load_config()
        ctx = kwargs.get("agent_context")
        self._primary_context = ctx in (None, "primary")

        self._session_id = session_id or "default"
        explicit_identity = kwargs.get("agent_identity")
        if explicit_identity == "default":
            explicit_identity = None
        self._agent_identity = (
            kwargs.get("gateway_session_key")
            or explicit_identity
            or kwargs.get("agent_workspace")
            or kwargs.get("agent_identity")
            or "default"
        )

        # Dedicated loop thread; methods are called synchronously from turn threads.
        self._loop = asyncio.new_event_loop()
        self._write_queue = asyncio.Queue(maxsize=self.config.queue_maxsize)
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.embedder = Embedder(
            url=self.config.embed_url,
            model=self.config.embed_model,
            dim=self.config.embed_dim,
        )

        try:
            self._run(self._ainit(), timeout=8.0)
            # Warm up embeddings so the first prefetch is fast.
            self._run(self.embedder.embed_text("warmup"), timeout=4.0)
        except Exception:
            logger.warning("hybrid-age init incomplete (DB or embedder unreachable)", exc_info=True)

        logger.info(
            "hybrid-age initialized session=%s identity=%s primary=%s",
            self._session_id, self._agent_identity, self._primary_context,
        )

    async def _ainit(self) -> None:
        import asyncpg

        pool = await asyncpg.create_pool(self.config.dsn, min_size=1, max_size=4)
        self.pool = pool
        self.store = Store(pool, graph_name=self.config.graph)
        async with pool.acquire() as conn:
            await self.store.load_age(conn)
        asyncio.create_task(self._awrite_drain())

    # -- turn capture (non-blocking enqueue) --------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Any = None,
    ) -> None:
        sid = session_id or self._session_id or "default"
        for role, content in (("user", user_content), ("assistant", assistant_content)):
            if not content or _is_noise(content):
                continue
            self._enqueue_write({
                "type": "turn",
                "session_id": sid,
                "role": role,
                "content": content[:8000],
            })
        self._track_turn(user_content, assistant_content)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory() writes to memory_entries. Non-blocking."""
        if target not in ("memory", "user"):
            return
        if action not in ("add", "replace", "remove"):
            return
        meta = dict(metadata or {})
        meta.setdefault("session_id", self._session_id)
        self._enqueue_write({
            "type": "memory",
            "action": action,
            "target": target,
            "content": content,
            "metadata": meta,
        })

    def _enqueue_write(self, item: dict) -> None:
        if not self._primary_context:
            return
        if self._loop is None or self._write_queue is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._put_nowait(item), self._loop)
            fut.result(timeout=0.5)
        except asyncio.QueueFull:
            self._dropped_writes += 1
            logger.warning("hybrid-age write queue full (dropped=%d)", self._dropped_writes)
        except Exception:
            logger.debug("enqueue failed", exc_info=True)

    async def _put_nowait(self, item: dict) -> None:
        assert self._write_queue is not None
        self._write_queue.put_nowait(item)

    async def _awrite_drain(self) -> None:
        while True:
            item = await self._write_queue.get()
            if item is None:
                break
            try:
                await self._awrite_item(item)
            except Exception:
                logger.debug("write item failed", exc_info=True)
            finally:
                self._write_queue.task_done()

    async def _awrite_item(self, item: dict) -> None:
        store = self.store
        embedder = self.embedder
        if store is None or embedder is None:
            return

        vec = await embedder.embed_text(item["content"])
        vec_literal = vec_to_literal(vec) if vec else None

        if item["type"] == "turn":
            await store.insert_turn(
                item["session_id"], self._agent_identity,
                item["role"], item["content"], vec_literal,
            )
        elif item["type"] == "memory":
            action = item["action"]
            target = item["target"]
            content = item["content"]
            metadata = item.get("metadata") or {}
            if action == "add":
                await store.upsert_memory_entry(
                    self._agent_identity, target, content, vec_literal, metadata,
                )
            elif action == "replace":
                old_text = metadata.get("old_text") or metadata.get("replaces")
                if old_text:
                    await store.replace_memory_entries(
                        self._agent_identity, target, old_text,
                        content, vec_literal, metadata,
                    )
                else:
                    await store.upsert_memory_entry(
                        self._agent_identity, target, content, vec_literal, metadata,
                    )
            elif action == "remove":
                await store.remove_memory_entries(
                    self._agent_identity, target, content,
                )

    # -- prefetch ----------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Sync recall. Never raises — returns "" on any failure."""
        if not (query or "").strip():
            return ""
        try:
            result = self._run(self._aprefetch(query), timeout=self.config.prefetch_timeout_s + 1.0)
            return result or ""
        except Exception:
            logger.exception("prefetch failed")
            return ""

    async def _aprefetch(self, query: str) -> str:
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(self.config.prefetch_timeout_s):
                enriched = self._enrich_query(query)
                emb = await self.embedder.embed_text(enriched[:800]) if self.embedder else None
                t_embed = time.perf_counter()
                seeds = await self.store.vector_search(vec_to_literal(emb), self.config.vector_k) \
                    if (emb and self.store) else []
                t_vec = time.perf_counter()
                graph_lines = await self._expand_graph(seeds)
                t_graph = time.perf_counter()
        except TimeoutError:
            logger.warning("prefetch timeout query=%r", query[:80])
            return ""

        items: List[dict] = []
        kept = 0
        for s in seeds:
            if s["similarity"] < self.config.min_similarity:
                continue
            if SECRET_RE.search(s["content"] or ""):
                continue
            items.append({"text": (s["content"] or "").strip(),
                          "score": s["similarity"], "kind": "fact"})
            kept += 1
        for line in graph_lines:
            items.append({"text": line, "score": 0.69, "kind": "graph"})

        items.sort(key=lambda x: x["score"], reverse=True)
        selected = self._budget(items)
        self._last_recall_count = len(selected)
        block = self._format(selected)
        logger.info(
            "prefetch seeds=%d graph=%d kept=%d chars=%d "
            "embed_ms=%.0f vector_ms=%.0f graph_ms=%.0f total_ms=%.0f",
            len(seeds), len(graph_lines), len(selected), len(block),
            (t_embed - t0) * 1000, (t_vec - t_embed) * 1000,
            (t_graph - t_vec) * 1000, (time.perf_counter() - t0) * 1000,
        )
        return block

    def _enrich_query(self, query: str) -> str:
        parts = [query]
        if self._recent_topics:
            parts.append("Topics: " + ", ".join(self._recent_topics[-5:]))
        if self._recent_turns:
            parts.append("Recent: " + self._recent_turns[-1][:150])
        return " | ".join(parts)

    async def _expand_graph(self, seeds: List[dict]) -> List[str]:
        from .provider_helpers import format_triple, parse_agtype_vertex  # noqa: F401

        if not seeds or self.store is None:
            return []
        chunk_ids = list({str(s["id"]) for s in seeds})
        vids_raw = await self.store.bridge_vertex_ids(chunk_ids)
        seed_ids: List[int] = []
        for vid in vids_raw:
            try:
                seed_ids.append(int(str(vid).strip('"')))
            except (ValueError, TypeError):
                continue
        if not seed_ids:
            return []

        rows = await self.store.expand_graph(
            seed_ids,
            ["RELATED_TO", "WORKS_ON", "USES", "DEPENDS_ON", "BUILT_WITH"],
            limit=40,
        )

        lines: List[str] = []
        seen: set[str] = set()
        for n, rel, m in rows:
            line = format_triple(n, rel, m)
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
        return lines[:8]

    def _budget(self, items: List[dict]) -> List[dict]:
        out: List[dict] = []
        used = 40
        cap = min(self.config.max_tokens, 1200)
        for it in items:
            text = it["text"]
            if not text:
                continue
            est = len(text) // 4 + 4
            if used + est > cap:
                continue
            out.append(it)
            used += est
        return out

    def _format(self, items: List[dict]) -> str:
        if not items:
            return ""
        graph = [i["text"] for i in items if i["kind"] == "graph"]
        facts = [i["text"] for i in items if i["kind"] != "graph"]
        lines = [
            "<memory_context>",
            "Relevant memory context (hybrid vector + graph):",
        ]
        for t in graph + facts:
            lines.append(f"- {t}")
        lines.append("</memory_context>")
        return "\n".join(lines)

    # -- misc ABC surface ----------------------------------------------------------

    def system_prompt_block(self) -> str:
        return "Hybrid AGE memory is active. Relevant facts are auto-injected each turn."

    def on_session_end(self, messages: Any) -> None:
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []  # pure context mode — no tools

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> Any:
        return {"error": "pure context mode — no tools"}

    # -- config schema (powers `hermes memory setup`) -------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [dict(f) for f in CONFIG_SCHEMA_FIELDS]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret behavior knobs to hybrid_age: in config.yaml."""
        import os

        path = os.path.join(hermes_home, "config.yaml")
        secret_keys = {f["key"] for f in CONFIG_SCHEMA_FIELDS if f.get("secret")}
        knobs = {k: v for k, v in (values or {}).items() if k not in secret_keys and v is not None}
        doc: Dict[str, Any] = {}
        yaml = None
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            pass
        except Exception:
            doc = {}
        existing = dict(doc.get("hybrid_age") or {}) if isinstance(doc.get("hybrid_age"), dict) else {}
        existing.update(knobs)
        doc["hybrid_age"] = existing
        os.makedirs(hermes_home, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            if yaml is not None:
                yaml.safe_dump(doc, fh, sort_keys=False)
            else:
                json.dump(doc, fh, indent=2)

    # -- shutdown ---------------------------------------------------------------

    def shutdown(self) -> None:
        """Drain queue <=5s, close pool, stop loop."""
        deadline = time.monotonic() + SHUTDOWN_DRAIN_S
        if self._loop is not None and self._write_queue is not None:
            try:
                loop = self._loop
                remaining = max(0.05, deadline - time.monotonic())
                done = threading.Event()

                async def _drain_then_stop() -> None:
                    try:
                        await asyncio.wait_for(
                            self._write_queue.join(), timeout=max(0.1, deadline - time.monotonic())
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    finally:
                        self._write_queue.put_nowait(None)  # stop sentinel
                        done.set()

                asyncio.run_coroutine_threadsafe(_drain_then_stop(), loop)
                done.wait(timeout=remaining)
            except Exception:
                logger.debug("drain on shutdown failed", exc_info=True)

        if self.pool is not None and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._pool_close(), self._loop
                ).result(timeout=max(0.2, deadline - time.monotonic()))
            except Exception:
                pass
        self.pool = None

        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    async def _pool_close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    # -- internals -----------------------------------------------------------------

    def _run(self, coro, timeout: float = 3.0):
        if self._loop is None:
            return asyncio.run(coro)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _track_turn(self, user_content: str, assistant_content: str) -> None:
        self._recent_turns.append(f"User: {(user_content or '')[:200]}")
        self._recent_turns.append(f"Assistant: {(assistant_content or '')[:200]}")
        if len(self._recent_turns) > self._max_turns:
            self._recent_turns = self._recent_turns[-self._max_turns:]
        self._extract_topics(user_content or "", assistant_content or "")

    def _extract_topics(self, *texts: str) -> None:
        for text in texts:
            for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", text):
                topic = match.group()
                if topic and topic not in self._recent_topics:
                    self._recent_topics.append(topic)
        if len(self._recent_topics) > self._max_topics:
            self._recent_topics = self._recent_topics[-self._max_topics:]
