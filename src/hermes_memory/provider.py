"""hermes_memory.provider — HybridAgeMemoryProvider(MemoryProvider).

Port of the production-proven plugin (~/.hermes/plugins/hybrid-age/provider.py)
to the packaged provider layout, per docs/plans/v0.1.md §3.1.

Contract highlights (Session/Turn flower + extract_nouns + SQL mentions):
- name = "hybrid-age"
- is_available(): no network — config resolvable + driver importable
- initialize(session_id, **kwargs): skip writes unless agent_context == "primary"
- prefetch(): sync, never raises, fenced block <= 1200 tokens, <2s warm
- sync_turn(): enqueue-only; asyncio.Queue(maxsize=256) with drop counting
- Drain records WriteOutcome on LEDGER (process-local; not Postgres)
- on_memory_write(action, target, content, metadata=None): exact kwarg
- get_config_schema() / save_config() power `hermes memory setup`
- shutdown(): drain queue <= 5s, close pool
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # running outside the Hermes runtime (tests, CI)
    MemoryProvider = object  # type: ignore[assignment,misc]

from .about_concepts import (  # noqa: F401 — re-export for existing tests
    SNAP_COSINE,
    _extract_concepts,
    _slug,
    is_one_word_concept,
    should_purge_concept,
)
from .config import CONFIG_SCHEMA_FIELDS, HybridAgeConfig, load_config
from .embed import Embedder, vec_to_literal
from .reception import ReceptionStore
from .schema_guard import apply_pending_migrations
from .session_kind import classify_session_kind
from .store import Store, clamp_hnsw_ef_search
from .turn_filter import _is_noise
from .write_outcome import DRAIN_COMPLETE, LEDGER, Kind, Stage, WriteOutcome

logger = logging.getLogger("hybrid_age")

SECRET_RE = re.compile(r"api[_-]?key|secret|password|BEGIN PRIVATE", re.I)

SHUTDOWN_DRAIN_S = 5.0


def _is_missing_embed_version_schema(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "undefinedcolumn" in name:
        return True
    return "embed_model" in msg or "embed_dim" in msg


class HybridAgeMemoryProvider(MemoryProvider):
    def __init__(self, config: Optional[HybridAgeConfig] = None) -> None:
        self.config = config or load_config()
        self.pool = None
        self.store: Optional[Store] = None
        self.embedder: Optional[Embedder] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._initialized = False
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
        self._last_turn_id: Dict[str, int] = {}

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

        # Idempotent: if already initialized, just refresh session state.
        if self._initialized:
            logger.info(
                "hybrid-age initialize called again; reusing existing loop/pool "
                "(session=%s identity=%s primary=%s)",
                self._session_id, self._agent_identity, self._primary_context,
            )
            return

        try:
            # Dedicated loop thread; methods are called synchronously from turn threads.
            self._loop = asyncio.new_event_loop()
            self._write_queue = asyncio.Queue(maxsize=self.config.queue_maxsize)
            self._drain_task = None
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
            self.embedder = Embedder(
                url=self.config.embed_url,
                model=self.config.embed_model,
                dim=self.config.embed_dim,
            )

            try:
                self._run(self._ainit(), timeout=8.0)
            except Exception as exc:
                logger.warning("hybrid-age init incomplete: %s", exc, exc_info=True)
                self._abandon_incomplete_init()
                return
            try:
                # Warm up embeddings so the first prefetch is fast.
                self._run(self.embedder.embed_text("warmup"), timeout=4.0)
            except Exception as exc:
                logger.warning("hybrid-age embed warmup failed: %s", exc, exc_info=True)

            self._initialized = True
            logger.info(
                "hybrid-age initialized session=%s identity=%s primary=%s",
                self._session_id, self._agent_identity, self._primary_context,
            )
        except BaseException:
            self._abandon_incomplete_init()
            raise

    def _abandon_incomplete_init(self) -> None:
        """Undo a failed initialize() so a later call can retry. Does not raise."""
        self._initialized = False
        self.store = None
        if self.pool is not None and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._pool_close(), self._loop
                ).result(timeout=1.0)
            except Exception:
                logger.warning("pool close after incomplete init failed", exc_info=True)
        self.pool = None
        self._write_queue = None
        self._drain_task = None
        loop = self._loop
        thread = self._thread
        self._loop = None
        self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                logger.debug("loop stop after incomplete init failed", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self.embedder = None

    async def _ainit(self) -> None:
        import asyncpg

        # Resolve the masked default DSN ({pg_password} / legacy ***) from
        # the environment so a bare default config still connects locally.
        dsn = self.config.dsn
        if '{pg_password}' in dsn or '***' in dsn:
            dsn = dsn.replace('{pg_password}', os.environ.get('HERMES_PG_PASSWORD', ''))
            dsn = dsn.replace('***', os.environ.get('HERMES_PG_PASSWORD', ''))
        await asyncio.to_thread(apply_pending_migrations, dsn)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        self.pool = pool  # assign before await points so failure paths can close it
        self.store = Store(
            pool,
            graph_name=self.config.graph,
            embed_model=self.config.embed_model,
            embed_dim=self.config.embed_dim,
            hnsw_ef_search=int(getattr(self.config, "hnsw_ef_search", 100)),
        )
        await self.store.require_schema_head()
        async with pool.acquire() as conn:
            await self.store.load_age(conn)
        # Strong reference so the drain loop is never garbage-collected mid-flight.
        self._drain_task = asyncio.create_task(self._awrite_drain())

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
                "previous_conversation_id": self._last_turn_id.get(sid),
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
        # C: auto-enrich librarian taxonomy so direct memory() writes never
        # violate the Data Taxonomy contract (doc_type/file_path/hash/
        # language/indexed_at). See issue #17: direct writes via the memory
        # tool previously persisted only {task_id, platform, ...} and left
        # hash/doc_type NULL, creating false-positive drift and zero bridge
        # rows. File-backed docs (via ingest) already carry file_path; user
        # prefs lack it and should default to user_preference.
        if not meta.get("hash"):
            meta["hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not meta.get("doc_type"):
            meta["doc_type"] = (
                "user_preference" if not meta.get("file_path") else "api_reference"
            )
        if not meta.get("language"):
            meta["language"] = "Text"
        if not meta.get("indexed_at"):
            meta["indexed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
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

            def _done(f) -> None:
                try:
                    f.result()
                except asyncio.QueueFull:
                    self._dropped_writes += 1
                    LEDGER.record(
                        WriteOutcome(
                            Stage.ENQUEUE,
                            Kind.DROPPED,
                            session_id=str(item.get("session_id") or ""),
                        )
                    )
                    logger.warning("hybrid-age write queue full (dropped=%d)", self._dropped_writes)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.warning("enqueue failed (callback)", exc_info=True)

            fut.add_done_callback(_done)
            # wait briefly for immediate QueueFull; TimeoutError means still
            # in flight — _done will handle a late QueueFull so we don't
            # miscount or block the turn thread (Copilot 3868002159 + Codex P2).
            fut.result(0.05)
        except asyncio.TimeoutError:
            logger.debug("enqueue in flight (queue not full)")
        except Exception:
            logger.warning("enqueue failed", exc_info=True)

    async def _put_nowait(self, item: dict) -> None:
        assert self._write_queue is not None
        self._write_queue.put_nowait(item)

    async def _awrite_drain(self) -> None:
        q = self._write_queue
        assert q is not None
        while True:
            item = await q.get()
            if item is None:
                break
            try:
                await self._awrite_item(item)
            except Exception as exc:
                if not item.get("_ledger_failed"):
                    stage = (
                        Stage.MEMORY_SQL if item.get("type") == "memory" else Stage.SQL_TURN
                    )
                    LEDGER.record(
                        WriteOutcome(
                            stage,
                            Kind.FAILED,
                            session_id=str(item.get("session_id") or ""),
                        ),
                        exc=exc,
                    )
                    item["_ledger_failed"] = True
                logger.warning("write item failed", exc_info=True)
            finally:
                q.task_done()

    async def _awrite_item(self, item: dict) -> None:
        store = self.store
        embedder = self.embedder
        if store is None or embedder is None:
            return

        if item["type"] == "turn":
            try:
                await self._awrite_turn(store, embedder, item)
            except Exception as exc:
                if not item.get("_ledger_failed"):
                    LEDGER.record(
                        WriteOutcome(
                            Stage.SQL_TURN,
                            Kind.FAILED,
                            session_id=str(item.get("session_id") or ""),
                        ),
                        exc=exc,
                    )
                    item["_ledger_failed"] = True
                logger.warning("turn write failed", exc_info=True)
            return

        embed_exc: BaseException | None = None
        try:
            vec = await embedder.embed_text(item["content"])
        except Exception as exc:
            logger.warning("memory embed failed", exc_info=True)
            vec = None
            embed_exc = exc
        if vec is None:
            LEDGER.record(
                WriteOutcome(
                    Stage.EMBED,
                    Kind.EMBED_NULL,
                    session_id=str(item.get("session_id") or ""),
                ),
                exc=embed_exc,
            )
        vec_literal = vec_to_literal(vec) if vec else None

        if item["type"] == "memory":
            action = item["action"]
            target = item["target"]
            content = item["content"]
            metadata = item.get("metadata") or {}
            try:
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
            except Exception as exc:
                LEDGER.record(
                    WriteOutcome(
                        Stage.MEMORY_SQL,
                        Kind.FAILED,
                        session_id=str(item.get("session_id") or ""),
                    ),
                    exc=exc,
                )
                item["_ledger_failed"] = True
                logger.warning("memory write failed", exc_info=True)

    async def _reception_stage(
        self, store: ReceptionStore, conv_id: int, session_id: str, role: str, content: str
    ) -> None:
        """Alias-on-write, user-span claims, uptake, and repair retraction.

        Never raises (caller guards too). Assistant spans yield no claims and
        no aliases: model speech stays unconfirmed until uptake says otherwise.
        """
        from .reception import (
            classify_repair,
            extract_alias_equations,
            extract_assertions,
        )

        if role != "user" or not (content or "").strip():
            return
        for surface, canon in extract_alias_equations(content):
            try:
                await store.insert_alias(surface, canon, "user_span")
            except Exception:
                logger.warning("alias write failed", exc_info=True)
        new_claims: list[dict] = []
        try:
            parsed = extract_assertions(content)
            if parsed:
                ids = await store.insert_claims(conv_id, parsed)
                for claim, cid in zip(parsed, ids):
                    claim["claim_id"] = cid
                new_claims = parsed
        except Exception:
            logger.warning("claim insert failed", exc_info=True)
        try:
            prior = await store.previous_turn(session_id, conv_id, role="assistant")
        except Exception:
            logger.warning("previous turn lookup failed", exc_info=True)
            return
        if prior is None:
            return
        verdict = classify_repair(str(prior.get("content") or ""), content)
        try:
            await store.write_uptake(int(prior["id"]), conv_id, verdict)
            if verdict == "repaired":
                await store.retract_on_repair(
                    int(prior["id"]), conv_id, content, new_claims
                )
        except Exception:
            logger.warning("uptake/retract failed", exc_info=True)

    async def _awrite_turn(self, store: Store, embedder: Embedder, item: dict) -> None:
        """Stages A–F. Never raises. B commits even if AGE / manifold fail."""
        from .extract_nouns import extract_nouns

        session_id = item.get("session_id") or ""
        content = item.get("content") or ""
        # Drain overwrites enqueue hint so the second item in one sync_turn
        # sees the first item's id. C reads only this field (no SQL lookup).
        item["previous_conversation_id"] = self._last_turn_id.get(session_id)
        embed_exc: BaseException | None = None
        try:
            vec = await embedder.embed_text(content)
        except Exception as exc:
            logger.warning("turn embed failed", exc_info=True)
            vec = None
            embed_exc = exc
        if vec is None:
            LEDGER.record(
                WriteOutcome(
                    Stage.EMBED,
                    Kind.EMBED_NULL,
                    session_id=session_id,
                ),
                exc=embed_exc,
            )
        vec_literal = vec_to_literal(vec) if vec else None

        try:
            conv_id = await store.insert_turn(
                session_id, self._agent_identity,
                item.get("role") or "user", content, vec_literal,
                metadata={"kind": classify_session_kind(session_id)},
            )
        except Exception as exc:
            if _is_missing_embed_version_schema(exc):
                logger.error(
                    "insert_turn failed: schema missing embed_model/embed_dim "
                    "(apply V10 before the next live insert)",
                    exc_info=True,
                )
            else:
                logger.warning("insert_turn failed", exc_info=True)
            LEDGER.record(
                WriteOutcome(
                    Stage.SQL_TURN,
                    Kind.FAILED,
                    session_id=session_id,
                ),
                exc=exc,
            )
            item["_ledger_failed"] = True
            return
        if conv_id is None:
            LEDGER.record(
                WriteOutcome(
                    Stage.SQL_TURN,
                    Kind.FAILED,
                    session_id=session_id,
                    detail="insert_turn returned None",
                )
            )
            item["_ledger_failed"] = True
            return
        self._last_turn_id[session_id] = int(conv_id)
        degraded = False
        await self._stamp_drain(
            store,
            int(conv_id),
            Kind.EMBED_NULL.value if vec is None else DRAIN_COMPLETE,
        )

        # Reception stage (spans/claims/uptake): never raises, never blocks drain.
        try:
            await self._reception_stage(
                store, int(conv_id), session_id, item.get("role") or "user", content
            )
        except Exception:
            logger.warning("reception stage failed", exc_info=True)

        vertex_id = None
        try:
            vertex_id = await self._link_turn_flower(
                store, int(conv_id), session_id, content,
                item.get("previous_conversation_id"),
            )
        except Exception as exc:
            logger.warning("flower MERGE failed", exc_info=True)
            LEDGER.record(
                WriteOutcome(
                    Stage.FLOWER,
                    Kind.GRAPH_DEGRADED,
                    session_id=session_id,
                    turn_id=int(conv_id),
                ),
                exc=exc,
            )
            degraded = True
            await self._stamp_drain(
                store, int(conv_id), Kind.GRAPH_DEGRADED.value,
            )
            vertex_id = None

        try:
            existing = []
            try:
                existing = await store.fetch_noun_labels()
            except Exception:
                logger.warning("fetch_noun_labels failed", exc_info=True)
            mentions = extract_nouns(
                content,
                existing_labels=existing,
                synthetic_session=str(session_id).startswith("verify-c5"),
            )
            noun_ids: list[int] = []
            if mentions:
                noun_ids = await store.write_noun_passports(
                    mentions,
                    chunk_id=f"conv_{int(conv_id)}",
                    source="conversation",
                    vertex_id=vertex_id,
                    session_id=session_id,
                    turn_id=int(conv_id),
                    graph_name=store.graph_name,
                )
        except Exception as exc:
            logger.warning("noun/passport write failed", exc_info=True)
            LEDGER.record(
                WriteOutcome(
                    Stage.NOUNS,
                    Kind.GRAPH_DEGRADED,
                    session_id=session_id,
                    turn_id=int(conv_id),
                ),
                exc=exc,
            )
            await self._stamp_drain(
                store, int(conv_id), Kind.GRAPH_DEGRADED.value,
            )
            return

        if vec is None or not mentions or len(noun_ids) < 2:
            if not degraded:
                await self._stamp_drain(
                    store,
                    int(conv_id),
                    Kind.EMBED_NULL.value if vec is None else DRAIN_COMPLETE,
                )
            return
        try:
            pairs: list[tuple[int, int]] = []
            src_vecs: dict[int, list[float]] = {}
            confs: dict[tuple[int, int], float] = {}
            for i in range(len(noun_ids) - 1):
                src_id, tgt_id = noun_ids[i], noun_ids[i + 1]
                pairs.append((src_id, tgt_id))
                confs[(src_id, tgt_id)] = float(mentions[i].conf)
                try:
                    src_vec = await embedder.embed_text(mentions[i].label)
                except Exception:
                    src_vec = None
                if src_vec:
                    src_vecs[src_id] = src_vec
            if pairs:
                await store.upsert_mentions_chain(
                    pairs,
                    turn_id=int(conv_id),
                    turn_vec=vec,
                    src_vecs=src_vecs,
                    confs=confs,
                )
        except Exception as exc:
            logger.warning("mentions chain failed", exc_info=True)
            LEDGER.record(
                WriteOutcome(
                    Stage.MENTIONS,
                    Kind.GRAPH_DEGRADED,
                    session_id=session_id,
                    turn_id=int(conv_id),
                ),
                exc=exc,
            )
            await self._stamp_drain(
                store, int(conv_id), Kind.GRAPH_DEGRADED.value,
            )
            return
        if not degraded:
            await self._stamp_drain(store, int(conv_id), DRAIN_COMPLETE)

    async def _stamp_drain(self, store: Store, turn_id: int, status: str) -> None:
        """Persist C–F outcome. Must not raise; stamp failure is not L1 FAILED."""
        try:
            await store.set_drain_status(turn_id, status)
        except Exception:
            logger.warning("drain_status stamp failed", exc_info=True)

    async def _link_turn_flower(
        self,
        store: Store,
        conv_id: int,
        session_id: str,
        content: str,
        previous_conversation_id: Any,
    ) -> int | None:
        try:
            await store.ensure_flower_labels()
        except Exception:
            logger.warning("ensure_flower_labels failed", exc_info=True)
        turn_content = (content or "")[:200]
        turn_props = {
            "name": f"turn_{conv_id}",
            "session_id": session_id,
            "turn_id": int(conv_id),
            "content": turn_content,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        try:
            vids_turn = await store.merge_vertices_batched([("Turn", turn_props)])
            turn_vid_str = vids_turn[0] if vids_turn else None
            if not turn_vid_str:
                return None
            turn_vid = int(turn_vid_str)
        except Exception:
            logger.warning("Turn vertex merge failed", exc_info=True)
            return None
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        edges: list[tuple[str, int, int]] = []
        edge_props: dict[tuple[str, int, int], dict] = {}
        if session_id:
            try:
                sess_vids = await store.merge_vertices_batched(
                    [("Session", {
                        "name": session_id,
                        "kind": classify_session_kind(session_id),
                        "created_at": now_iso,
                    })]
                )
                sess_vid = int(sess_vids[0]) if sess_vids and sess_vids[0] else None
                if sess_vid:
                    edges.append(("IN_SESSION", turn_vid, sess_vid))
                    edge_props[("IN_SESSION", turn_vid, sess_vid)] = {
                        "weight": 1.0, "cosine": 1.0, "created_at": now_iso,
                    }
            except Exception:
                logger.warning("Session vertex merge failed", exc_info=True)
            prev_id = previous_conversation_id
            if prev_id:
                try:
                    prev_vids = await store.merge_vertices_batched(
                        [("Turn", {"name": f"turn_{int(prev_id)}"})]
                    )
                    prev_vid = int(prev_vids[0]) if prev_vids and prev_vids[0] else None
                    if prev_vid:
                        edges.append(("NEXT", prev_vid, turn_vid))
                        edge_props[("NEXT", prev_vid, turn_vid)] = {
                            "weight": 1.0, "cosine": 1.0, "created_at": now_iso,
                        }
                except Exception:
                    logger.warning("NEXT edge failed", exc_info=True)
        if edges:
            try:
                await store.merge_edges_batched(edges, edge_props=edge_props)
            except Exception:
                logger.warning("turn flower edge merge failed", exc_info=True)
        return turn_vid

    # -- prefetch ----------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Sync recall. Never raises — returns "" on any failure."""
        if not (query or "").strip():
            return ""
        try:
            result = self._run(self._aprefetch(query), timeout=self.config.prefetch_timeout_s + 1.0)
            return result or ""
        except Exception:
            # Read-path failure. Same return as empty recall (""). Log is the distinguish.
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
                logger.info(
                    "prefetch k=%s hnsw.ef_search=%s seeds=%s",
                    self.config.vector_k,
                    clamp_hnsw_ef_search(getattr(self.store, "hnsw_ef_search", 100)),
                    len(seeds),
                )
                kept_seeds = []
                for s in seeds:
                    if s["similarity"] < self.config.min_similarity:
                        continue
                    if SECRET_RE.search(s["content"] or ""):
                        continue
                    kept_seeds.append(s)
                paths = await self._expand_paths(kept_seeds, q_vec=emb)
                t_graph = time.perf_counter()
        except TimeoutError:
            logger.warning("prefetch timeout query=%r", query[:80])
            return ""

        from .provider_helpers import format_injection, format_span_injection

        seed_turn_ids: set[int] = set()
        for s in kept_seeds:
            if s.get("src") != "conversation":
                continue
            raw = str(s.get("id") or "").removeprefix("conv_")
            if raw.isdigit():
                seed_turn_ids.add(int(raw))

        extra_ids: list[int] = []
        extra_seen: set[int] = set()
        for p in paths:
            for tid in p.get("provenance_turns") or []:
                try:
                    n = int(tid)
                except (TypeError, ValueError):
                    continue
                if n in seed_turn_ids or n in extra_seen:
                    continue
                extra_seen.add(n)
                extra_ids.append(n)
        bodies: dict[int, dict] = {}
        fetcher = getattr(self.store, "conversations_by_ids", None)
        if extra_ids and callable(fetcher):
            for row in await fetcher(extra_ids):
                try:
                    bodies[int(row["id"])] = row
                except (TypeError, ValueError, KeyError):
                    continue
        for p in paths:
            if p.get("content"):
                continue
            for tid in p.get("provenance_turns") or []:
                try:
                    row = bodies.get(int(tid))
                except (TypeError, ValueError):
                    continue
                if not row or not (row.get("content") or "").strip():
                    continue
                p["content"] = str(row.get("content") or "").strip()
                p["turn_id"] = int(tid)
                p["session_id"] = row.get("session_id")
                p["created_at"] = row.get("ts")
                break

        seed_payload = []
        for s in kept_seeds:
            chunk_id = (
                str(s.get("id") or "")
                if str(s.get("id") or "").startswith("conv_")
                else f"conv_{s.get('id')}"
            )
            attached = (
                [p for p in paths if p.get("chunk_id") == chunk_id]
                if s.get("src") == "conversation"
                else []
            )
            best_w = best_c = best_decay = best_score = None
            if attached:
                p0 = attached[0]
                best_w = p0.get("weight")
                best_c = p0.get("cosine")
                best_decay = p0.get("decay")
                best_score = p0.get("score")
            turn_id = None
            raw_id = str(s.get("id") or "").removeprefix("conv_")
            if s.get("src") == "conversation" and raw_id.isdigit():
                turn_id = int(raw_id)
            seed_payload.append({
                "score": s["similarity"],
                "src": s.get("src"),
                "id": s.get("id"),
                "content": (s.get("content") or "").strip(),
                "paths": attached[:3],
                "session_id": s.get("session_id"),
                "created_at": s.get("ts") or s.get("created_at"),
                "turn_id": turn_id,
                "best_weight": best_w,
                "best_cosine": best_c,
                "best_decay": best_decay,
                "best_score": best_score,
            })
        seed_payload.sort(key=lambda x: x["score"], reverse=True)
        selected = self._budget_seeds(seed_payload)
        self._last_recall_count = len(selected)
        # Provenance-first packing: speaker + ±1 neighbor per hit, two bins.
        # Neighbors are packed context (never embedded); uptake stays unknown
        # until the classifier runs. Revert to format_injection(selected) below
        # to restore seed+path rendering.
        from collections.abc import Awaitable, Callable

        neighbor_fetcher: Callable[[list], Awaitable[dict]] | None = getattr(
            self.store, "conversations_neighbors", None
        )
        neighbor_map: dict = {}
        if selected and callable(neighbor_fetcher):
            try:
                hit_turn_ids = [s["turn_id"] for s in selected if s.get("turn_id")]
                neighbor_map = await neighbor_fetcher(hit_turn_ids)
            except Exception:
                logger.exception("neighbor fetch failed")
                neighbor_map = {}
        for s in selected:
            if s.get("src") in ("doc_chunk", "memory_entry"):
                s["role"] = "doc"
                continue
            nb = neighbor_map.get(s.get("turn_id")) or {}
            if nb.get("self_role"):
                s["role"] = nb["self_role"]
            for key in ("prev", "next"):
                row = nb.get(key) or {}
                body = str(row.get("content") or "")
                if not body.strip() or SECRET_RE.search(body):
                    continue
                s[key] = {
                    "turn_id": row.get("turn_id"),
                    "role": row.get("role"),
                    "content": body,
                    "ts": row.get("ts"),
                }
        from .tokens import injection_token_cap

        block = format_span_injection(
            selected, token_budget=injection_token_cap(self.config.max_tokens)
        )
        if not (block or "").strip():
            logger.info("prefetch empty recall")
        graph_n = sum(len(s.get("paths") or []) for s in selected)
        logger.info(
            "prefetch seeds=%d graph=%d kept=%d chars=%d "
            "embed_ms=%.0f vector_ms=%.0f graph_ms=%.0f total_ms=%.0f",
            len(seeds), graph_n, len(selected), len(block),
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

    async def _expand_paths(
        self,
        seeds: List[dict],
        *,
        q_vec: list[float] | None,
    ) -> List[dict]:
        from .provider_helpers import format_triple, parse_agtype_vertex
        from .walk import WalkHypothesis, parse_embedding

        if not seeds or self.store is None or not q_vec:
            return []

        conversation_seeds = [s for s in seeds if s.get("src") == "conversation"]
        conv_ids = [
            int(str(s["id"]).removeprefix("conv_"))
            for s in conversation_seeds
            if str(s.get("id") or "").removeprefix("conv_").isdigit()
        ]
        passports = await self.store.passports_for_conversations(conv_ids)
        seeds_by_chunk = {
            (
                str(s["id"])
                if str(s["id"]).startswith("conv_")
                else f"conv_{s['id']}"
            ): s
            for s in conversation_seeds
        }

        hypotheses: list[WalkHypothesis] = []
        for passport in passports:
            seed = seeds_by_chunk.get(str(passport.get("chunk_id") or ""))
            if seed is None:
                continue
            chunk_vec = parse_embedding(seed.get("embedding"))
            if not chunk_vec:
                continue
            hypotheses.append(
                WalkHypothesis(
                    noun_id=int(passport["noun_id"]),
                    chunk_id=str(passport["chunk_id"]),
                    session_id=str(passport.get("session_id") or ""),
                    turn_id=int(passport["turn_id"]),
                    sim=float(seed["similarity"]),
                    chunk_vec=chunk_vec,
                )
            )
        if not hypotheses:
            return []
        rows = await self.store.expand_graph(
            hypotheses,
            q_vec=q_vec,
            hops=2,
            k=int(getattr(self.config, "vector_k", 8)),
        )
        paths: List[dict] = []
        seen: set[str] = set()
        for row in rows:
            audit = getattr(row, "audit", {}) or {}
            n, rel, m = row[0], row[1], row[2]
            w = float(row[3]) if len(row) > 3 else 0.5
            c = float(row[4]) if len(row) > 4 else 0.5
            decay = float(row[5]) if len(row) > 5 else 0.5
            if len(row) <= 6:
                continue
            score = float(row[6])
            triple = format_triple(n, rel, m)
            if not triple or "->" not in triple:
                continue
            triple = f"{triple} w={w:.2f} c={c:.2f}"
            dedup_key = (
                f"{audit.get('chunk_id')}:{audit.get('turn_id')}:"
                f"{audit.get('hop')}:{triple}"
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            src = parse_agtype_vertex(n) or {}
            dst = parse_agtype_vertex(m) or {}
            paths.append({
                "from_vid": str(src.get("id") or ""),
                "from_name": str(src.get("name") or ""),
                "to_name": str(dst.get("name") or ""),
                "triple": triple,
                "rel": "" if rel is None else str(rel).strip('"'),
                "weight": w,
                "cosine": c,
                "decay": decay,
                "score": score,
                "session_id": audit.get("session_id"),
                "turn_id": audit.get("turn_id"),
                "chunk_id": audit.get("chunk_id"),
                "hop": audit.get("hop"),
                "provenance_turns": list(audit.get("provenance_turns") or []),
            })
        return paths[:16]

    def _budget_seeds(self, seeds: List[dict]) -> List[dict]:
        from .tokens import count_tokens, injection_token_cap

        out: List[dict] = []
        used = 40
        cap = injection_token_cap(self.config.max_tokens)
        for s in seeds:
            excerpt = (s.get("content") or "")[:400]
            est = count_tokens(excerpt) + 8
            if used + est > cap:
                continue
            out.append(s)
            used += est
        return out

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
        """Write non-secret behavior knobs to hybrid_age: in config.yaml.
        
        Atomic write (temp file + os.rename) preserving unrelated keys on partial update.
        """
        import os
        import tempfile

        path = os.path.join(hermes_home, "config.yaml")
        secret_keys = {f["key"] for f in CONFIG_SCHEMA_FIELDS if f.get("secret")}
        knobs = {k: v for k, v in (values or {}).items() if k not in secret_keys and v is not None}
        doc: Dict[str, Any] = {}
        yaml_mod = None
        try:
            import yaml

            yaml_mod = yaml
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
        # Atomic write: temp file + rename to avoid partial/corrupt writes
        fd, temp_path = tempfile.mkstemp(dir=hermes_home, prefix=".config.yaml.", suffix=".tmp", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if yaml_mod is not None:
                    yaml_mod.safe_dump(doc, fh, sort_keys=False)
                else:
                    json.dump(doc, fh, indent=2)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            raise

    # -- shutdown ---------------------------------------------------------------

    def shutdown(self) -> None:
        """Drain queue <=5s, close pool, stop loop."""
        deadline = time.monotonic() + SHUTDOWN_DRAIN_S
        if self._loop is not None and self._write_queue is not None:
            try:
                loop = self._loop
                q = self._write_queue
                remaining = max(0.05, deadline - time.monotonic())
                done = threading.Event()

                async def _drain_then_stop() -> None:
                    try:
                        await asyncio.wait_for(
                            q.join(), timeout=max(0.1, deadline - time.monotonic())
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    finally:
                        q.put_nowait(None)  # stop sentinel
                        done.set()

                asyncio.run_coroutine_threadsafe(_drain_then_stop(), loop)
                done.wait(timeout=remaining)
            except Exception:
                logger.debug("drain on shutdown failed", exc_info=True)

        # Only close the pool if the drain completed cleanly; if writes are
        # still in flight, closing here would yank connections out from under
        # them. The loop teardown below handles cleanup in that case.
        drained = self._write_queue is None or self._write_queue.empty()
        if not drained:
            logger.warning(
                "hybrid-age shutdown: write queue still busy after %ss drain — "
                "abandoning %d queued write(s); pool left for loop teardown",
                SHUTDOWN_DRAIN_S, self._write_queue.qsize() if self._write_queue else 0,
            )
        elif (
            self.pool is not None
            and self._loop is not None
            and self._loop.is_running()
        ):
            try:
                asyncio.run_coroutine_threadsafe(
                    self._pool_close(), self._loop
                ).result(timeout=max(0.2, deadline - time.monotonic()))
            except Exception:
                logger.debug("pool close on shutdown failed", exc_info=True)
        self.pool = None

        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                logger.debug("loop stop failed on shutdown", exc_info=True)

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
