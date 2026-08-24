"""Hermes MemoryProvider: hybrid pgvector + Apache AGE auto-recall.

Drop into ~/.hermes/plugins/hybrid-age/provider.py (plus __init__.py register).
Prefetch is sync, best-effort, never raises into the agent loop.
Writes are non-blocked via asyncio.Queue + background task on a dedicated event loop.

Env:
  HYBRID_AGE_DSN          postgres://hermes:hermesdev@localhost:5440/hermes
  HYBRID_AGE_EMBED_URL    http://localhost:11434/v1
  HYBRID_AGE_EMBED_MODEL  nomic-embed-text
  HYBRID_AGE_GRAPH        hermes_knowledge
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import asyncpg
from openai import AsyncOpenAI

from agent.memory_provider import MemoryProvider

logger = logging.getLogger("hybrid_age")

DSN = os.environ.get(
    "HYBRID_AGE_DSN",
    "postgres://hermes:hermesdev@localhost:5440/hermes",
)
EMBED_URL = os.environ.get("HYBRID_AGE_EMBED_URL", "http://localhost:11434/v1")
EMBED_MODEL = os.environ.get("HYBRID_AGE_EMBED_MODEL", "nomic-embed-text")
GRAPH = os.environ.get("HYBRID_AGE_GRAPH", "hermes_knowledge")

VECTOR_K = 12
MIN_SIM = 0.50
MAX_TOKENS = 6000
PREFETCH_TIMEOUT_S = 2.0
SECRET_RE = re.compile(r"api[_-]?key|secret|password|BEGIN PRIVATE", re.I)

# Noise filter — matches pgvector defaults
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


def _is_noise(content: str, *, min_chars: int = TURN_MIN_CHARS) -> bool:
    """True for short / boilerplate content we don't want in recall."""
    stripped = (content or "").strip()
    if not stripped:
        return True
    if len(stripped) < min_chars:
        return True
    return bool(_NOISE_RE.match(stripped))


class HybridAgeMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self.ollama: AsyncOpenAI | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session_id = ""
        self._agent_identity = ""
        # Write queue for non-blocking sync_turn + on_memory_write
        self._write_queue: "asyncio.Queue[Optional[dict]]" = None  # type: ignore
        # Session context — rolling window of recent topics for query enrichment
        self._recent_topics: List[str] = []
        self._recent_turns: List[str] = []
        self._max_topics = 8
        self._max_turns = 6

    def _track_turn(self, user_content: str, assistant_content: str) -> None:
        """Track recent turns for session context."""
        self._recent_turns.append(f"User: {user_content[:200]}")
        self._recent_turns.append(f"Assistant: {assistant_content[:200]}")
        # Keep only the last N turns
        if len(self._recent_turns) > self._max_turns:
            self._recent_turns = self._recent_turns[-self._max_turns:]
        # Extract topics from this turn
        self._extract_topics(user_content, assistant_content)

    def _extract_topics(self, *texts: str) -> None:
        """Extract potential topic keywords from text."""
        import re
        for text in texts:
            # Find capitalized phrases (likely entities)
            for match in re.finditer(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b', text):
                topic = match.group()
                if topic and topic not in self._recent_topics:
                    self._recent_topics.append(topic)
        # Keep only the most recent topics
        if len(self._recent_topics) > self._max_topics:
            self._recent_topics = self._recent_topics[-self._max_topics:]

    def _get_session_context(self) -> str:
        """Build a context string from recent turns and topics."""
        parts = []
        if self._recent_topics:
            parts.append(f"Recent topics: {', '.join(self._recent_topics[-5:])}")
        if self._recent_turns:
            parts.append("Recent conversation:")
            for turn in self._recent_turns[-4:]:
                parts.append(f"  {turn[:150]}")
        return "\n".join(parts)

    @property
    def name(self) -> str:
        return "hybrid-age"

    def is_available(self) -> bool:
        return bool(DSN)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
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

        # Single event loop for both prefetch and writes
        self._loop = asyncio.new_event_loop()
        self._write_queue = asyncio.Queue(maxsize=256)
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._run(self._ainit())

        # Warm up Ollama
        try:
            self._run(self._embed("warmup"))
        except Exception:
            pass

        logger.info("hybrid-age initialized session=%s identity=%s", session_id, self._agent_identity)

    async def _ainit(self) -> None:
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        self.ollama = AsyncOpenAI(base_url=EMBED_URL, api_key="ollama")
        async with self.pool.acquire() as conn:
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, public;")
        # Start the write drain as a background task
        asyncio.create_task(self._awrite_drain())

    # ----- Turn capture (non-blocking) ----------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Any = None,
    ) -> None:
        """Persist a completed turn. Non-blocking — enqueues to background writer."""
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
        # Track this turn for session context
        self._track_turn(user_content, assistant_content)

    # ----- Built-in memory mirror (non-blocking) -----------------------

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

    # ----- Write queue drain -------------------------------------------

    def _enqueue_write(self, item: dict) -> None:
        """Enqueue a write. Drops if queue full (never blocks the agent loop)."""
        try:
            self._loop.call_soon_threadsafe(self._write_queue.put_nowait, item)
        except Exception:
            logger.warning("hybrid-age write queue full; dropping write")

    async def _awrite_drain(self) -> None:
        """Background drain: embeds + writes from the queue."""
        while True:
            item = await self._write_queue.get()
            if item is None:  # shutdown sentinel
                break
            try:
                await self._awrite_item(item)
            except Exception:
                logger.debug("write item failed", exc_info=True)
            finally:
                self._write_queue.task_done()

    async def _awrite_item(self, item: dict) -> None:
        """Execute a single write (turn or memory)."""
        if not self.pool:
            return

        if item["type"] == "turn":
            vec = await self._embed(item["content"])
            vec_literal = self._vec_to_literal(vec) if vec else None
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO conversations
                        (session_id, agent_identity, role, content, embedding, metadata)
                    VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                    """,
                    item["session_id"],
                    self._agent_identity,
                    item["role"],
                    item["content"],
                    vec_literal,
                    json.dumps({}),
                )
        elif item["type"] == "memory":
            action = item["action"]
            target = item["target"]
            content = item["content"]
            metadata = item.get("metadata") or {}

            vec = await self._embed(content)
            vec_literal = self._vec_to_literal(vec) if vec else None

            async with self.pool.acquire() as conn:
                if action == "add":
                    await conn.execute(
                        """
                        INSERT INTO memory_entries
                            (agent_identity, target, content, embedding, metadata)
                        VALUES ($1, $2, $3, $4::vector, $5::jsonb)
                        ON CONFLICT (agent_identity, target, content) DO NOTHING
                        """,
                        self._agent_identity,
                        target,
                        content,
                        vec_literal,
                        json.dumps(metadata),
                    )
                elif action == "replace":
                    old_text = metadata.get("old_text") or metadata.get("replaces")
                    if old_text:
                        n = await conn.execute(
                            """
                            UPDATE memory_entries
                               SET content = $1,
                                   embedding = $2::vector,
                                   updated_at = now()
                             WHERE agent_identity = $3
                               AND target = $4
                               AND content LIKE $5
                            """,
                            content,
                            vec_literal,
                            self._agent_identity,
                            target,
                            f"%{old_text}%",
                        )
                        if n == "UPDATE 0":
                            await conn.execute(
                                """
                                INSERT INTO memory_entries
                                    (agent_identity, target, content, embedding, metadata)
                                VALUES ($1, $2, $3, $4::vector, $5::jsonb)
                                """,
                                self._agent_identity,
                                target,
                                content,
                                vec_literal,
                                json.dumps(metadata),
                            )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO memory_entries
                                (agent_identity, target, content, embedding, metadata)
                            VALUES ($1, $2, $3, $4::vector, $5::jsonb)
                            """,
                            self._agent_identity,
                            target,
                            content,
                            vec_literal,
                            json.dumps(metadata),
                        )
                elif action == "remove":
                    await conn.execute(
                        """
                        DELETE FROM memory_entries
                         WHERE agent_identity = $1
                           AND target = $2
                           AND content LIKE $3
                        """,
                        self._agent_identity,
                        target,
                        f"%{content}%",
                    )

    def _vec_to_literal(self, vec: list) -> str:
        """Convert a Python list to pgvector literal: '[0.1,0.2,...]'."""
        return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"

    # ----- Prefetch (sync, fast, never throws) -------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query.strip() or self.pool is None:
            return ""
        try:
            return self._run(self._aprefetch(query)) or ""
        except Exception:
            logger.exception("prefetch failed")
            return ""

    def on_session_end(self, messages: Any) -> None:
        return None

    def get_tool_schemas(self) -> list:
        return []

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs: Any) -> Any:
        return {"error": "pure context mode — no tools"}

    def system_prompt_block(self) -> str:
        return "Hybrid AGE memory is active. Relevant facts are auto-injected each turn."

    def shutdown(self) -> None:
        if self._loop and self._write_queue:
            self._loop.call_soon_threadsafe(self._write_queue.put_nowait, None)
        if self.pool is not None:
            self._run(self.pool.close())
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ----- internals -------------------------------------------------

    def _run(self, coro):
        if self._loop is None:
            return asyncio.run(coro)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=PREFETCH_TIMEOUT_S + 1.0)

    async def _aprefetch(self, query: str) -> str:
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(PREFETCH_TIMEOUT_S):
                # Enrich the query with session context
                enriched_query = self._enrich_query(query)
                emb = await self._embed(enriched_query[:800])
                t_embed = time.perf_counter()
                seeds = await self._vector_search(emb)
                t_vec = time.perf_counter()
                graph_lines = await self._expand_graph(seeds)
                t_graph = time.perf_counter()
        except TimeoutError:
            logger.warning("prefetch timeout query=%r", query[:80])
            return ""

        items = []
        for s in seeds:
            if s["similarity"] < MIN_SIM:
                continue
            if SECRET_RE.search(s["content"] or ""):
                continue
            items.append(
                {
                    "text": (s["content"] or "").strip(),
                    "score": s["similarity"],
                    "kind": "fact",
                }
            )
        for line in graph_lines:
            items.append({"text": line, "score": 0.6 * 1.15, "kind": "graph"})

        items.sort(key=lambda x: x["score"], reverse=True)
        selected = self._budget(items)
        block = self._format(selected)
        logger.info(
            "prefetch seeds=%d graph=%d kept=%d chars=%d embed_ms=%.0f vector_ms=%.0f graph_ms=%.0f total_ms=%.0f",
            len(seeds),
            len(graph_lines),
            len(selected),
            len(block),
            (t_embed - t0) * 1000,
            (t_vec - t_embed) * 1000,
            (t_graph - t_vec) * 1000,
            (time.perf_counter() - t0) * 1000,
        )
        return block

    def _enrich_query(self, query: str) -> str:
        """Enrich the query with session context for better retrieval."""
        parts = [query]
        # Add recent topics
        if self._recent_topics:
            parts.append("Topics: " + ", ".join(self._recent_topics[-5:]))
        # Add a snippet of recent turns for context
        if self._recent_turns:
            parts.append("Recent: " + self._recent_turns[-1][:150])
        return " | ".join(parts)

    async def _embed(self, text: str) -> list:
        """Embed text with Ollama. Returns Python list (not string)."""
        resp = await self.ollama.embeddings.create(model=EMBED_MODEL, input=text)
        return resp.data[0].embedding

    async def _vector_search(self, vec: list) -> list[dict]:
        """Search memory_entries first, fall back to conversations if sparse."""
        vec_literal = self._vec_to_literal(vec)
        sql_entries = """
            SELECT id::text AS id, content,
                   1 - (embedding <=> $1::vector) AS similarity,
                   'memory_entry' AS source
            FROM memory_entries
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """
        sql_conversations = """
            SELECT id::text AS id, content,
                   1 - (embedding <=> $1::vector) AS similarity,
                   'conversation' AS source
            FROM conversations
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql_entries, vec_literal, VECTOR_K)
            if not rows:
                rows = await conn.fetch(sql_conversations, vec_literal, VECTOR_K)
        return [dict(r) for r in rows]

    async def _expand_graph(self, seeds: list[dict]) -> list[str]:
        """Expand graph from seed chunks via memory_chunk_nodes bridge table."""
        if not seeds:
            return []

        chunk_ids = list({s["id"] for s in seeds})
        if not chunk_ids:
            return []

        async with self.pool.acquire() as conn:
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, public;")

            verts = await conn.fetch(
                """
                SELECT DISTINCT vertex_id::text AS vid
                FROM memory_chunk_nodes
                WHERE chunk_id = ANY($1::text[])
                """,
                chunk_ids,
            )
            if not verts:
                return []

            seed_ids = []
            for r in verts:
                vid = r["vid"]
                try:
                    seed_ids.append(int(vid.strip('"')))
                except (ValueError, TypeError):
                    continue

            if not seed_ids:
                return []

            ids_literal = "[" + ", ".join(str(i) for i in seed_ids) + "]"
            cypher = f"""
                SELECT * FROM cypher('{GRAPH}', $$
                    MATCH (n)
                    WHERE id(n) IN {ids_literal}
                    OPTIONAL MATCH (n)-[r]->(m)
                    WHERE type(r) IN ['RELATED_TO', 'WORKS_ON', 'USES', 'DEPENDS_ON', 'BUILT_WITH']
                    RETURN n, type(r) AS rel, m
                    LIMIT 40
                $$) AS (n agtype, rel agtype, m agtype)
            """
            try:
                rows = await conn.fetch(cypher)
            except Exception:
                logger.exception("cypher expand failed")
                return []

        lines: list[str] = []
        seen: set[str] = set()
        for row in rows:
            line = _format_triple(row["n"], row["rel"], row["m"])
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
        return lines[:8]

    def _budget(self, items: list[dict]) -> list[dict]:
        out: list[dict] = []
        used = 40
        for it in items:
            text = it["text"]
            if not text:
                continue
            est = len(text) // 4 + 4
            if used + est > MAX_TOKENS:
                continue
            out.append(it)
            used += est
        return out

    def _format(self, items: list[dict]) -> str:
        if not items:
            return ""
        graph = [i["text"] for i in items if i["kind"] == "graph"]
        facts = [i["text"] for i in items if i["kind"] != "graph"]
        lines = ["Relevant memory context (hybrid vector + graph):"]
        for t in graph + facts:
            lines.append(f"- {t}")
        return "\n".join(lines)


def _parse_agtype_vertex(blob: Any) -> Optional[Dict]:
    """Parse an AGE vertex agtype into a Python dict."""
    if blob is None:
        return None
    raw = blob if isinstance(blob, str) else str(blob)
    for suffix in ("::vertex", "::edge", "::path"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _format_triple(n: Any, rel: Any, m: Any) -> str:
    def name(blob: Any) -> Optional[str]:
        data = _parse_agtype_vertex(blob)
        if data is None:
            return None
        props = data.get("properties") or {}
        label = data.get("label") or ""
        nm = props.get("name") or props.get("summary") or ""
        if not nm:
            return None
        return f"{label} {nm}".strip() if label else str(nm)

    a, b = name(n), name(m)
    if not a:
        return ""
    rel_s = "" if rel is None else str(rel).strip('"')
    if b and rel_s and rel_s not in ("None", "null"):
        return f"[{a}] -{rel_s}-> [{b}]"
    return f"[{a}]"


def register(ctx) -> None:
    ctx.register_memory_provider(HybridAgeMemoryProvider())
