"""hermes_memory.store — SQL/Cypher data access layer.

Rules (plan §3.3):
- Every Cypher statement runs inside a SAVEPOINT; a failed statement rolls back
  to its savepoint and never poisons the surrounding transaction.
- MERGE on the minimal unique key ({name} for entities, {path} for files);
  non-key properties set via post-MERGE ``SET v += {props}`` (AGE 1.6 lacks
  ``ON CREATE SET``/``ON MATCH SET``; see plan §3.3).
- MERGEs are batched (>=50 statements per transaction) per apache/age#2177.
- Vertex IDs crossing into JS/UI are stringified at this boundary.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncpg

from .age_cypher import (  # noqa: F401 — re-export public + test aliases
    _SAFE_IDENT,
    _check_label,
    _pick_cypher_dollar_tag,
    _pick_dollar_tag,
    _savepoint_name,
    age_props,
    age_str,
    check_label,
    cypher_call,
    cypher_dollar_quote,
    savepoint_name,
    validate_graph_name,
)
from .embed import vec_to_literal
from .schema_guard import expected_versions_for_head_check, missing_versions
from .store_concepts import StoreConceptsMixin
from .store_expand import StoreExpandMixin
from .store_merge import StoreMergeMixin
from .store_vec import _cosine_similarity, _parse_pgvector  # noqa: F401

try:
    from psycopg.sql import SQL, Identifier
except ImportError:
    Identifier = None  # type: ignore[assignment]
    SQL = None  # type: ignore[assignment]

logger = logging.getLogger("hybrid_age.store")

# Re-export Cypher helpers so existing ``from hermes_memory.store import …``
# call sites keep working. Definitions live in age_cypher.py.
__all_cypher__ = (
    "age_str",
    "age_props",
    "check_label",
    "validate_graph_name",
    "cypher_call",
    "cypher_dollar_quote",
    "savepoint_name",
    "_SAFE_IDENT",
    "_check_label",
    "_savepoint_name",
    "_pick_cypher_dollar_tag",
    "_pick_dollar_tag",
)


def bridge_keys_for_seed(seed: Dict[str, Any]) -> List[str]:
    """Namespace-scoped chunk_id keys for a vector_search hit.

    ``memory_entries`` and ``conversations`` use independent sequences, so
    id=42 can be both a file row and a turn. Look up only the keys for
    ``seed['src']``; never mix ``conv_N`` with canonical/mem_ N.
    """
    i = str(seed.get("id") or "")
    if not i:
        return []
    src = str(seed.get("src") or "")
    if src == "conversation":
        return [i] if i.startswith("conv_") else [f"conv_{i}"]
    if src == "doc_chunk":
        return [i]
    if src == "memory_entry":
        keys = [i]
        if i.isdigit():
            keys.append(f"mem_{i}")
        elif i.startswith("mem_") and i[4:].isdigit():
            keys.append(i[4:])
        return list(dict.fromkeys(keys))
    if i.startswith("conv_"):
        return [i]
    if ":" in i:
        return [i]
    keys = [i]
    if i.isdigit():
        keys.append(f"mem_{i}")
    return keys


def _escape_like(text: str) -> str:
    r"""Escape LIKE wildcards (\ % _) for use with ESCAPE '\'."""
    return (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def dedup_key(name: str, label: str) -> Tuple[str, str]:
    """Canonical vertex identity for MERGE/backfill dedup (public API).

    Entities merge on lowercased, whitespace-trimmed name within their label.
    This is the single derivation used by backfill dedup; the property suite
    in tests/property/test_p8_backfill_dedup.py pins it against this function
    rather than a local copy.
    """
    return (name.strip().lower(), label)


def compaction_keepers(pairs):
    """Deterministic dedup: keeper = min(id) for each connected component.

    pairs: iterable of (keeper_candidate, loser_candidate) or (a,b,cosine).
    Returns dict loser->keeper with deterministic ordering (smaller id wins).
    Overlapping pairs are transitively merged via Union-Find.
    """
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    ids = set()
    plist = []
    for p in pairs:
        a, b = int(p[0]), int(p[1])
        ids.add(a)
        ids.add(b)
        plist.append((a, b))
        if a not in parent:
            parent[a] = a
        if b not in parent:
            parent[b] = b
    for a, b in plist:
        union(a, b)
    result = {}
    groups = {}
    for nid in ids:
        r = find(nid)
        groups.setdefault(r, []).append(nid)
    for root, members in groups.items():
        members_sorted = sorted(members)
        keeper = members_sorted[0]
        for m in members_sorted[1:]:
            result[m] = keeper
    return result


# -- Recency decay helpers (issue #38) ----------------------------------------

def _parse_created_at(raw: Any) -> Optional[str]:
    """Normalize an agtype created_at value to a plain ISO string or None."""
    if raw is None:
        return None
    s = str(raw).strip().strip('"').strip("'")
    if not s or s == "null":
        return None
    return s

def recency_decay(
    created_at: Optional[str],
    half_life_days: float = 30.0,
    now: Optional[datetime.datetime] = None,
) -> float:
    """Compute recency decay exp(-age_days/half_life) with fallback 0.5 for legacy.

    - created_at: ISO string or None. If None/missing/unparseable -> 0.5.
    - half_life_days: denominator for exp decay (default 30).
    - now: reference time for deterministic tests; defaults to utcnow.
    - Future dates clamp age to 0 -> decay 1.0.
    """
    if not created_at:
        return 0.5
    try:
        iso = str(created_at).replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        if now is None:
            now_dt = datetime.datetime.now(datetime.timezone.utc)
        else:
            now_dt = now
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
        age_days = (now_dt - dt).total_seconds() / 86400.0
        if age_days < 0:
            age_days = 0
        hl = float(half_life_days) if half_life_days else 30.0
        if hl <= 0:
            return 0.5
        decay = math.exp(-age_days / hl)
        if decay < 0:
            return 0.0
        if decay > 1:
            return 1.0
        return float(decay)
    except Exception:
        return 0.5

def recency_decay_for_edge(
    edge_created_at: Optional[str],
    vertex_created_at: Optional[str],
    half_life_days: float = 30.0,
    now: Optional[datetime.datetime] = None,
) -> float:
    """Prefer edge created_at, fallback to target vertex created_at, else 0.5."""
    primary = _parse_created_at(edge_created_at)
    if primary:
        return recency_decay(primary, half_life_days, now)
    fallback = _parse_created_at(vertex_created_at)
    if fallback:
        return recency_decay(fallback, half_life_days, now)
    return 0.5


def embedding_dim_of(vec_literal: Optional[str]) -> Optional[int]:
    """Count floats in a pgvector text literal. None if missing/unparseable."""
    if not vec_literal:
        return None
    text = str(vec_literal).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return 0
    parts = [p for p in text.split(",") if p.strip()]
    try:
        [float(p) for p in parts]
    except ValueError:
        return None
    return len(parts)


def assert_embedding_compatible(vec_literal: Optional[str], embed_dim: int) -> None:
    dim = embedding_dim_of(vec_literal)
    if dim is not None and dim != int(embed_dim):
        raise ValueError(f"embedding dim {dim} != {embed_dim}")


def clamp_hnsw_ef_search(ef: int) -> int:
    """HNSW `ef_search` is silent when too low; keep it in a logged, bounded range."""
    return max(10, min(int(ef), 400))


def hnsw_ef_search_sql(ef: int) -> str:
    return f"SET LOCAL hnsw.ef_search = {clamp_hnsw_ef_search(ef)}"


class Store(StoreExpandMixin, StoreMergeMixin, StoreConceptsMixin):
    """Async data access over the pgvector + AGE schema."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        graph_name: str = "hermes_knowledge",
        *,
        embed_model: str = "nomic-embed-text",
        embed_dim: int = 768,
        hnsw_ef_search: int = 100,
    ):
        # Validated once here; the f-string cypher wrappers in this module
        # interpolate ONLY this vetted value (issue #10).
        self.graph_name = validate_graph_name(graph_name)
        self.pool = pool
        self.embed_model = embed_model
        self.embed_dim = int(embed_dim)
        self.hnsw_ef_search = clamp_hnsw_ef_search(hnsw_ef_search)

    async def require_schema_head(self) -> None:
        """Refuse to start if migration_history is still behind after apply.

        Process start runs ``apply_pending_migrations`` first (no-op when
        ``sql/migrations`` is not next to the plugin). This check is the
        backstop: clone/CI compare disk V*.sql; installed plugin compares
        ``CONSUMER_HEAD_VERSIONS`` to the live ``migration_history`` table.
        """
        expected = expected_versions_for_head_check()
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch("SELECT version FROM migration_history")
            except Exception as exc:
                raise RuntimeError(
                    "migration_history is missing; run python scripts/migrate.py"
                ) from exc
        applied = [str(r["version"]) for r in rows]
        missing = missing_versions(applied, expected)
        if missing:
            raise RuntimeError(
                f"migration_history missing {missing}; "
                "run python scripts/migrate.py before writing "
                f"(applied={sorted(applied)})"
            )
        await self.require_embed_version_columns()

    async def require_embed_version_columns(self) -> None:
        """Fail loudly if V10 `embed_model`/`embed_dim` columns are missing.

        INSERT lists those columns; a pre-V10 schema would otherwise raise
        UndefinedColumn inside the drain, which used to be logged at debug
        and look like a successful write.
        """
        async with self.pool.acquire() as conn:
            n = await conn.fetchval(
                """
                SELECT count(*) FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'conversations'
                   AND column_name IN ('embed_model', 'embed_dim')
                """
            )
        if int(n or 0) != 2:
            raise RuntimeError(
                "conversations is missing embed_model/embed_dim; "
                "apply sql/migrations/V10__embed_model_version.sql before writing"
            )

    async def load_age(self, conn) -> None:
        await conn.execute("LOAD 'age';")
        await conn.execute("SET search_path = ag_catalog, public;")

    # -- Turn / memory writes -------------------------------------------------

    async def insert_turn(
        self,
        session_id: str,
        agent_identity: str,
        role: str,
        content: str,
        vec_literal: Optional[str],
        metadata: Dict[str, Any] | None = None,
    ) -> Optional[int]:
        """Insert a turn and return its id (RETURNING id) for graph linkage.

        Idempotent on retry: same session+role+bytes within a trailing
        10-minute window returns the existing id instead of double-inserting
        (crashed-and-retried drain, double-delivered sync_turn). A user
        repeating a sentence next week is a NEW span — the window is what
        separates a retry from a re-utterance. History is grandfathered:
        pre-existing duplicates stay as-is. Below 80 chars the lookup is
        skipped entirely: short acknowledgements are always new spans.
        """
        assert_embedding_compatible(vec_literal, self.embed_dim)
        import hashlib

        dup = None
        if len(content or "") >= 80:
            content_hash = hashlib.md5((content or "").encode("utf-8")).hexdigest()
            async with self.pool.acquire() as conn:
                dup = await conn.fetchval(
                    """
                    SELECT id FROM conversations
                     WHERE session_id = $1 AND role = $2 AND md5(content) = $3
                       AND ts > now() - interval '10 minutes'
                     ORDER BY id DESC LIMIT 1
                    """,
                    session_id, role, content_hash,
                )
            if dup is not None:
                logger.info(
                    "span dedupe hit session=%s role=%s id=%s", session_id, role, dup
                )
                return int(dup)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations
                    (session_id, agent_identity, role, content, embedding, metadata,
                     embed_model, embed_dim)
                VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb, $7, $8)
                RETURNING id
                """,
                session_id,
                agent_identity,
                role,
                content,
                vec_literal,
                json.dumps(metadata or {}),
                self.embed_model,
                self.embed_dim,
            )
            return int(row["id"]) if row and row["id"] is not None else None

    async def set_drain_status(self, turn_id: int, status: str) -> None:
        """Stamp this drain's C–F outcome on an existing row (V11).

        Prospective. Not the V9 historical gap (row never got a passport).
        L1 insert failure has no row to stamp.
        """
        from .write_outcome import DRAIN_STATUSES

        if status not in DRAIN_STATUSES:
            raise ValueError(f"invalid drain_status {status!r}")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET drain_status = $2 WHERE id = $1",
                int(turn_id),
                status,
            )

    async def count_unpassported_turns(self) -> int:
        """V9 historical gap: row exists, never got a conversation passport.

        Not drain_status (prospective: this drain's C–F after insert B).
        Same SQL as schema_guard.UNPASSPORTED_TURNS_SQL — one formula.
        """
        from .schema_guard import UNPASSPORTED_TURNS_SQL

        async with self.pool.acquire() as conn:
            n = await conn.fetchval(UNPASSPORTED_TURNS_SQL)
        return int(n or 0)

    async def ensure_about_labels(self) -> None:
        """Ensure Turn/Concept vertices and ABOUT edge labels exist.

        Each create_* runs inside a SAVEPOINT so 'already exists' never poisons
        the surrounding transaction; other errors are logged but not raised.
        """
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            try:
                async with conn.transaction():
                    for idx, (kind, label) in enumerate(
                        [
                            ("v", "Turn"),
                            ("v", "Concept"),
                            ("v", "Session"),
                            ("e", "ABOUT"),
                            ("e", "NEXT"),
                            ("e", "IN_SESSION"),
                        ]
                    ):
                        sp = savepoint_name(f"ensure_{label.lower()}", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            if kind == "v":
                                await conn.execute(
                                    "SELECT create_vlabel($1, $2)", self.graph_name, label
                                )
                            else:
                                await conn.execute(
                                    "SELECT create_elabel($1, $2)", self.graph_name, label
                                )
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception as exc:
                            try:
                                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            except asyncpg.PostgresError:
                                pass
                            try:
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                            except asyncpg.PostgresError:
                                pass
                            if "already exists" not in str(exc).lower():
                                logger.warning("ensure label %s failed", label, exc_info=True)
            except Exception:
                logger.warning("ensure_about_labels transaction error", exc_info=True)

    async def bridge_turn(self, conv_id: int, vertex_id: int) -> None:
        """Bridge a conversation Turn vertex: conv_{id} -> Turn vertex."""
        chunk_id = f"conv_{int(conv_id)}"
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (chunk_id, source, vertex_id) DO NOTHING
                    """,
                    chunk_id,
                    "conversation",
                    int(vertex_id),
                    self.graph_name,
                )
            except Exception:
                logger.warning("bridge_turn failed", exc_info=True)

    async def previous_conversation_id(self, session_id: str, conv_id: int) -> Optional[int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT id FROM conversations
                 WHERE session_id = $1 AND id < $2
                 ORDER BY id DESC
                 LIMIT 1
                """,
                session_id,
                int(conv_id),
            )
        return int(row) if row is not None else None

    async def upsert_noun(self, label: str, type: str | None = None) -> int:
        async with self.pool.acquire() as conn:
            return await self._upsert_noun_on_conn(conn, label, type)

    async def _upsert_noun_on_conn(self, conn, label: str, type: str | None = None) -> int:
        row = await conn.fetchrow(
            """
            INSERT INTO noun (label, type)
            VALUES ($1, $2)
            ON CONFLICT (label) DO UPDATE SET
              type = COALESCE(EXCLUDED.type, noun.type)
            RETURNING id
            """,
            label,
            type,
        )
        return int(row["id"])

    async def write_passports(self, rows: list[dict]) -> None:
        if not rows:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._write_passports_on_conn(conn, rows)

    async def _write_passports_on_conn(self, conn, rows: list[dict]) -> None:
        for row in rows:
            noun_id = row.get("noun_id")
            if noun_id is None:
                raise ValueError("conversation passports require noun_id")
            await conn.execute(
                """
                INSERT INTO memory_chunk_nodes
                    (chunk_id, source, noun_id, vertex_id, session_id, turn_id, conf, graph_name)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (chunk_id, source, noun_id) WHERE noun_id IS NOT NULL
                DO UPDATE SET
                  conf = EXCLUDED.conf,
                  vertex_id = COALESCE(memory_chunk_nodes.vertex_id, EXCLUDED.vertex_id),
                  session_id = EXCLUDED.session_id
                """,
                row["chunk_id"],
                row["source"],
                int(noun_id),
                row.get("vertex_id"),
                row.get("session_id"),
                row.get("turn_id"),
                row.get("conf"),
                row.get("graph_name") or self.graph_name,
            )

    async def write_noun_passports(
        self,
        mentions: Sequence[Any],
        *,
        chunk_id: str,
        source: str,
        vertex_id: int | None,
        session_id: str,
        turn_id: int,
        graph_name: str | None = None,
    ) -> list[int]:
        """D+E in one transaction: upsert nouns then passports."""
        if not mentions:
            return []
        ids: list[int] = []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                passport_rows: list[dict] = []
                for m in mentions:
                    nid = await self._upsert_noun_on_conn(
                        conn, m.label, getattr(m, "type", None),
                    )
                    ids.append(nid)
                    passport_rows.append({
                        "chunk_id": chunk_id,
                        "source": source,
                        "noun_id": nid,
                        "vertex_id": vertex_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "conf": float(getattr(m, "conf", 0.3)),
                        "graph_name": graph_name or self.graph_name,
                    })
                await self._write_passports_on_conn(conn, passport_rows)
        return ids

    async def upsert_mentions_chain(
        self,
        pairs: list[tuple[int, int]],
        *,
        turn_id: int,
        turn_vec: list[float],
        src_vecs: dict[int, list[float]],
        confs: dict[tuple[int, int], float],
    ) -> None:
        if not pairs or turn_vec is None:
            return
        turn_lit = vec_to_literal(turn_vec)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for src, tgt in pairs:
                    src_vec = src_vecs.get(src)
                    if not src_vec:
                        continue
                    conf = float(confs.get((src, tgt), 0.3))
                    if conf <= 0:
                        continue
                    existing = await conn.fetchrow(
                        """
                        SELECT e_tgt_vec, magnitude, provenance_turns
                          FROM semantic_edge
                         WHERE src_noun=$1 AND tgt_noun=$2 AND verb_type='mentions'
                        """,
                        int(src),
                        int(tgt),
                    )
                    if existing is None:
                        await conn.execute(
                            """
                            INSERT INTO semantic_edge (
                                src_noun, tgt_noun, verb_type,
                                e_src_vec, e_tgt_vec, magnitude, polarity,
                                last_active_turn, last_active_ts, provenance_turns
                            )
                            VALUES (
                                $1, $2, 'mentions',
                                $3::vector, $4::vector, $5, 1,
                                $6, now(), ARRAY[$6]::bigint[]
                            )
                            """,
                            int(src),
                            int(tgt),
                            vec_to_literal(src_vec),
                            turn_lit,
                            min(8.0, conf),
                            int(turn_id),
                        )
                        continue
                    mag = min(8.0, float(existing["magnitude"]) + conf)
                    alpha = min(0.4, max(0.05, conf))
                    old_tgt = _parse_pgvector(existing["e_tgt_vec"])
                    if len(old_tgt) != len(turn_vec):
                        raise ValueError(
                            "semantic_edge dim mismatch: stored "
                            f"{len(old_tgt)} vs turn {len(turn_vec)} "
                            f"(src_noun={src} tgt_noun={tgt})"
                        )
                    ema = [(1.0 - alpha) * o + alpha * t for o, t in zip(old_tgt, turn_vec)]
                    prov = list(existing["provenance_turns"] or [])
                    if int(turn_id) not in prov:
                        prov.append(int(turn_id))
                    prov = prov[:32]
                    await conn.execute(
                        """
                        UPDATE semantic_edge
                           SET magnitude = $3,
                               e_tgt_vec = $4::vector,
                               last_active_turn = $5,
                               last_active_ts = now(),
                               provenance_turns = $6::bigint[]
                         WHERE src_noun=$1 AND tgt_noun=$2 AND verb_type='mentions'
                        """,
                        int(src),
                        int(tgt),
                        mag,
                        vec_to_literal(ema),
                        int(turn_id),
                        prov,
                    )

    async def passports_for_conversations(self, conv_ids: Sequence[int]) -> list[dict]:
        if not conv_ids:
            return []
        keys = [f"conv_{int(i)}" for i in conv_ids]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.chunk_id, p.source, p.noun_id, p.vertex_id, p.session_id,
                       p.turn_id, p.conf, p.graph_name, n.label, n.type
                  FROM memory_chunk_nodes p
                  JOIN noun n ON n.id = p.noun_id
                 WHERE p.chunk_id = ANY($1::text[])
                   AND p.source = 'conversation'
                   AND p.noun_id IS NOT NULL
                """,
                keys,
            )
        return [dict(r) for r in rows]

    async def conversations_by_ids(self, ids: Sequence[int]) -> list[dict]:
        """Turn bodies for injection extras. Empty input → no round trip."""
        if not ids:
            return []
        wanted = []
        seen: set[int] = set()
        for raw in ids:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if n in seen:
                continue
            seen.add(n)
            wanted.append(n)
        if not wanted:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, session_id, content, ts, role,
                       drain_status, embed_model, embed_dim
                  FROM conversations
                 WHERE id = ANY($1::bigint[])
                """,
                wanted,
            )
        return [dict(r) for r in rows]

    async def conversations_neighbors(self, ids: Sequence[int]) -> dict[int, dict]:
        """Prev/next span in the same session per turn id. Empty input → no round trip.

        Ordering is (ts, id) within session — the same sequence the span fixture
        uses. Returns {turn_id: {"prev": row|None, "next": row|None}} where each
        row carries turn_id/content/role/ts. Pure SELECT, no writes.
        """
        wanted: list[int] = []
        seen: set[int] = set()
        for raw in ids or []:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if n in seen:
                continue
            seen.add(n)
            wanted.append(n)
        if not wanted:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH ordered AS (
                  SELECT id, session_id, content, ts, role,
                         LAG(id) OVER w AS prev_id,
                         LEAD(id) OVER w AS next_id
                    FROM conversations
                   WHERE session_id IN (
                         SELECT DISTINCT session_id FROM conversations
                          WHERE id = ANY($1::bigint[])
                         )
                  WINDOW w AS (PARTITION BY session_id ORDER BY ts, id)
                )
                SELECT o.id AS turn_id, o.role AS self_role,
                       p.id AS prev_id, p.role AS prev_role,
                       p.content AS prev_content, p.ts AS prev_ts,
                       n.id AS next_id, n.role AS next_role,
                       n.content AS next_content, n.ts AS next_ts
                  FROM ordered o
                  LEFT JOIN conversations p ON p.id = o.prev_id
                  LEFT JOIN conversations n ON n.id = o.next_id
                 WHERE o.id = ANY($1::bigint[])
                """,
                wanted,
            )
        out: dict[int, dict] = {}
        for r in rows:
            d = dict(r)
            tid = int(d["turn_id"])
            prev = None
            if d.get("prev_id") is not None:
                prev = {
                    "turn_id": int(d["prev_id"]),
                    "role": d.get("prev_role"),
                    "content": d.get("prev_content"),
                    "ts": str(d.get("prev_ts")),
                }
            nxt = None
            if d.get("next_id") is not None:
                nxt = {
                    "turn_id": int(d["next_id"]),
                    "role": d.get("next_role"),
                    "content": d.get("next_content"),
                    "ts": str(d.get("next_ts")),
                }
            out[tid] = {"prev": prev, "next": nxt, "self_role": d.get("self_role")}
        return out

    async def fetch_noun_labels(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT label FROM noun")
        return [r["label"] for r in rows]

    # -- Reception: aliases, uptake, claims, retraction -----------------------
    # Write path for the provenance-first contract. conversations rows are the
    # spans; these methods only add overlay rows. All no-op safe pre-migration
    # only in the sense that callers guard every call (tables come from V12).

    async def previous_turn(
        self, session_id: str, conv_id: int, *, role: str = "assistant"
    ) -> dict | None:
        """Latest in-session turn before conv_id with the given role (time order)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, content FROM conversations
                 WHERE session_id = $1 AND role = $2
                   AND (ts, id) < (SELECT ts, id FROM conversations WHERE id = $3)
                 ORDER BY ts DESC, id DESC LIMIT 1
                """,
                session_id, role, int(conv_id),
            )
        return dict(row) if row is not None else None

    async def insert_alias(self, surface_norm: str, canon_id: str, source: str) -> bool:
        """Explicit-equation alias only. True if a new live mapping won.

        Refuses self-maps and cycles: if canon_id resolves transitively back
        to surface_norm, the row would corrupt the dictionary (A→B→A), so the
        span stays unaliased and the collision is logged for manual review.
        Renaming is a retract of the old alias + a new live mapping, never an
        overwrite (partial unique index enforces one live canon per surface).
        """
        if surface_norm == canon_id:
            return False
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT surface_norm, canon_id FROM aliases WHERE valid = 'live'"
            )
            amap = {r["surface_norm"]: r["canon_id"] for r in rows}
            cur, chain = canon_id, [surface_norm]
            for _ in range(8):
                nxt = amap.get(cur)
                if nxt is None:
                    break
                if nxt in chain:
                    logger.warning(
                        "alias cycle refused %s -> %s (chain %s)",
                        surface_norm, canon_id, chain,
                    )
                    return False
                chain.append(cur)
                cur = nxt
            row = await conn.fetchrow(
                """
                INSERT INTO aliases (surface_norm, canon_id, source)
                VALUES ($1, $2, $3)
                ON CONFLICT (surface_norm) WHERE valid = 'live' DO NOTHING
                RETURNING alias_id
                """,
                surface_norm, canon_id, source,
            )
        return row is not None

    async def alias_map(self) -> dict[str, str]:
        """Live alias map: surface_norm → canon_id. Small table, one SELECT."""
        amap, _ = await self.alias_graph()
        return amap

    async def alias_graph(
        self,
    ) -> tuple[dict[str, str], dict[str, tuple]]:
        """Live alias dictionary plus canon-birth metadata.

        meta[canon] = (first_seen, min_alias_id): earliest live row that
        ESTABLISHED the slug as a canon_id (not merely touched it as a
        surface). Survivor elections (never lex-smallest: lex promotes
        nicknames like "te" over "tokyo eye") prefer the oldest canon birth —
        canons are born as equation left-hand sides, so oldest-birth is
        earliest-LHS — then drop slugs under 4 chars when a longer live
        alias exists in the same component.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT surface_norm, canon_id, created_at, alias_id
                     FROM aliases WHERE valid = 'live'"""
            )
        amap: dict[str, str] = {}
        meta: dict[str, tuple] = {}
        for r in rows:
            amap[r["surface_norm"]] = r["canon_id"]
            key = (r["created_at"], int(r["alias_id"]))
            if r["canon_id"] not in meta or key < meta[r["canon_id"]]:
                meta[r["canon_id"]] = key
        return amap, meta

    @staticmethod
    def _graph(rows) -> tuple[dict[str, str], dict[str, tuple]]:
        """Build (amap, meta) from live alias rows on an already-held connection."""
        amap: dict[str, str] = {}
        meta: dict[str, tuple] = {}
        for r in rows:
            amap[r["surface_norm"]] = r["canon_id"]
            key = (r["created_at"], int(r["alias_id"]))
            if r["canon_id"] not in meta or key < meta[r["canon_id"]]:
                meta[r["canon_id"]] = key
        return amap, meta

    @staticmethod
    def _elect(members: list[str], meta: dict[str, tuple] | None) -> str:
        """Cycle survivor: oldest canon birth wins; short slugs lose to longer
        live aliases; never lexicographic (lex elects nicknames). Slugs with
        no canon birth (surface-only) sort as newest."""
        from datetime import datetime, timezone

        pool = [m for m in members if len(m) >= 4] or list(members)
        if not meta:
            return pool[0]
        far = (datetime.max.replace(tzinfo=timezone.utc), 10**18)
        return min(pool, key=lambda m: meta.get(m, far))

    @staticmethod
    def _resolve(
        text: str, amap: dict[str, str], meta: dict[str, tuple] | None = None
    ) -> tuple[str, bool]:
        """Canon for text through the live map. Returns (canon, cycled).

        Transitive with a depth cap. A cycle collapses via _elect (oldest
        equation, short slugs demoted — never lex). Unresolved names fall
        back to their own normalization: distinct surfaces stay distinct,
        never forged equal.
        """
        from .reception import normalize_surface

        cur = normalize_surface(text)
        seen = [cur]
        for _ in range(8):
            nxt = amap.get(cur)
            if nxt is None or nxt == cur:
                return cur, False
            if nxt in seen:
                return Store._elect(seen[seen.index(nxt):] + [nxt], meta), True
            seen.append(nxt)
            cur = nxt
        return cur, False

    @staticmethod
    def _canon(
        text: str, amap: dict[str, str], meta: dict[str, tuple] | None = None
    ) -> str:
        return Store._resolve(text, amap, meta)[0]

    async def insert_claims(self, span_id: int, claims: Sequence[dict]) -> list[int]:
        """Insert user-span claims; inherit the span's own verdict if classified.

        One live row per proposition: the partial unique key
        (subject_canon, verb, object_canon, polarity) WHERE valid='live'
        merges repeats (span_id moves to the latest saying, seen_count bumps).
        A live opposite-polarity row with a non-repaired verdict abstains —
        the new row goes in as valid='unknown', never a second live truth.
        Same subject+verb with a different object, or a different verb, is a
        second row: objects stay opaque, never fused, never fuzzy-matched.
        """
        if not claims:
            return []
        async with self.pool.acquire() as conn:
            uptake = await conn.fetchval(
                """SELECT value FROM uptakes
                    WHERE prior_span_id = $1 OR next_span_id = $1
                    ORDER BY created_at DESC LIMIT 1""",
                int(span_id),
            )
            verdict = uptake or "unknown"
            amap, meta = Store._graph(await conn.fetch(
                """SELECT surface_norm, canon_id, created_at, alias_id
                     FROM aliases WHERE valid = 'live'"""
            ))
            ids: list[int] = []
            for c in claims:
                sub = self._canon(c["subject"], amap, meta)
                obj = self._canon(c["object"], amap, meta)
                verb = c["verb"]
                pol = c.get("polarity", "positive")
                opp = await conn.fetchval(
                    """SELECT claim_id FROM claims
                        WHERE valid = 'live' AND span_id <> $4
                          AND subject_canon = $1 AND lower(verb) = lower($2)
                          AND object_canon = $3 AND polarity <> $5
                        LIMIT 1""",
                    sub, verb, obj, int(span_id), pol,
                )
                if opp is not None and verdict != "repaired":
                    # Abstain: keep the audit trail, keep it out of the live set.
                    prior_row = await conn.fetchval(
                        """SELECT claim_id FROM claims
                            WHERE span_id = $1 AND subject_canon = $2
                              AND lower(verb) = lower($3) AND object_canon = $4
                              AND polarity = $5
                            LIMIT 1""",
                        int(span_id), sub, verb, obj, pol,
                    )
                    if prior_row is not None:
                        ids.append(int(prior_row))
                        continue
                    cid = await conn.fetchval(
                        """
                        INSERT INTO claims
                            (span_id, subject, verb, object, polarity, act,
                             uptake, subject_canon, object_canon, valid)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'unknown')
                        RETURNING claim_id
                        """,
                        int(span_id), c["subject"], verb, c["object"], pol,
                        c.get("act", "assert"), verdict, sub, obj,
                    )
                    ids.append(int(cid))
                    continue
                cid = await conn.fetchval(
                    """
                    INSERT INTO claims
                        (span_id, subject, verb, object, polarity, act, uptake,
                         subject_canon, object_canon)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (subject_canon, verb, object_canon, polarity)
                        WHERE valid = 'live'
                    DO UPDATE SET span_id = EXCLUDED.span_id,
                                  seen_count = claims.seen_count + 1
                    RETURNING claim_id
                    """,
                    int(span_id), c["subject"], verb, c["object"], pol,
                    c.get("act", "assert"), verdict, sub, obj,
                )
                ids.append(int(cid))
        return ids

    async def write_uptake(
        self, prior_span_id: int, next_span_id: int, value: str
    ) -> None:
        """Write reception once; copy onto the prior span's claims (no downgrade).

        A later pass may only upgrade unknown → repaired, never the reverse
        without a new user span.
        """
        async with self.pool.acquire() as conn:
            inserted = await conn.fetchrow(
                """
                INSERT INTO uptakes (prior_span_id, next_span_id, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (prior_span_id, next_span_id) DO NOTHING
                RETURNING uptake_id
                """,
                int(prior_span_id), int(next_span_id), value,
            )
            if inserted is None and value == "repaired":
                await conn.execute(
                    """UPDATE uptakes SET value = 'repaired'
                        WHERE prior_span_id = $1 AND next_span_id = $2
                          AND value = 'unknown'""",
                    int(prior_span_id), int(next_span_id),
                )
            row = await conn.fetchrow(
                """SELECT value FROM uptakes
                    WHERE prior_span_id = $1 AND next_span_id = $2""",
                int(prior_span_id), int(next_span_id),
            )
            if row is not None:
                await conn.execute(
                    """UPDATE claims SET uptake = $1
                        WHERE span_id = $2 AND uptake = 'unknown'""",
                    row["value"], int(prior_span_id),
                )

    async def retract_on_repair(
        self, prior_span_id: int, next_span_id: int,
        next_text: str, new_claims: Sequence[dict],
    ) -> int:
        """Retract live claims contradicted by a repaired user turn. Returns count.

        Polarity-opposite match on normalized (subject, verb, object), excluding
        the repairing span itself. Deictic repairs with no new assertion retract
        the prior span's live claims with a NULL superseder. Never deletes.
        """
        from .reception import is_deictic_repair

        retracted = 0
        async with self.pool.acquire() as conn:
            amap, meta = Store._graph(await conn.fetch(
                """SELECT surface_norm, canon_id, created_at, alias_id
                     FROM aliases WHERE valid = 'live'"""
            ))
            if new_claims:
                for c in new_claims:
                    if c.get("act") != "assert" or not c.get("claim_id"):
                        continue
                    # Resolve through the CURRENT map on both sides: aliases
                    # are retroactive (a later "TE = Tokyo Eye" unifies
                    # history). Stored canon columns are the write-time cache.
                    new_sub = self._canon(c["subject"], amap, meta)
                    new_obj = self._canon(c["object"], amap, meta)
                    cands = await conn.fetch(
                        """SELECT claim_id, subject, object FROM claims
                            WHERE valid = 'live' AND span_id <> $1
                              AND lower(verb) = lower($2)
                              AND polarity <> $3""",
                        int(next_span_id), c["verb"],
                        c.get("polarity", "positive"),
                    )
                    for r in cands:
                        if (self._canon(r["subject"], amap, meta) != new_sub
                                or self._canon(r["object"], amap, meta) != new_obj):
                            continue
                        res = await conn.execute(
                            """UPDATE claims SET valid = 'retracted',
                                      superseded_by = $1
                                WHERE claim_id = $2 AND valid = 'live'""",
                            int(c["claim_id"]), int(r["claim_id"]),
                        )
                        try:
                            retracted += int(str(res).split()[-1])
                        except (ValueError, IndexError):
                            pass
            elif is_deictic_repair(next_text):
                res = await conn.execute(
                    """UPDATE claims SET valid = 'retracted'
                        WHERE span_id = $1 AND valid = 'live'""",
                    int(prior_span_id),
                )
                try:
                    retracted += int(str(res).split()[-1])
                except (ValueError, IndexError):
                    pass
        return retracted

    async def span_overlays(self, ids: Sequence[int]) -> dict[int, dict]:
        """Per-hit overlay for packing: uptake verdict (as prior span),
        live-claim count, and retracted claim subjects. Pure SELECTs."""
        wanted: list[int] = []
        seen: set[int] = set()
        for raw in ids or []:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if n in seen:
                continue
            seen.add(n)
            wanted.append(n)
        out: dict[int, dict] = {
            n: {"uptake": None, "live_claims": 0, "retracted": []} for n in wanted
        }
        if not wanted:
            return out
        async with self.pool.acquire() as conn:
            for r in await conn.fetch(
                """SELECT prior_span_id, value FROM uptakes
                    WHERE prior_span_id = ANY($1::bigint[])""",
                wanted,
            ):
                out[int(r["prior_span_id"])]["uptake"] = r["value"]
            for r in await conn.fetch(
                """SELECT span_id,
                          count(*) FILTER (WHERE valid = 'live') AS live
                     FROM claims WHERE span_id = ANY($1::bigint[])
                     GROUP BY span_id""",
                wanted,
            ):
                out[int(r["span_id"])]["live_claims"] = int(r["live"])
            for r in await conn.fetch(
                """SELECT span_id, subject, verb, object FROM claims
                    WHERE span_id = ANY($1::bigint[]) AND valid = 'retracted'
                    LIMIT 12""",
                wanted,
            ):
                lst = out[int(r["span_id"])]["retracted"]
                if len(lst) < 2:
                    lst.append({
                        "subject": r["subject"],
                        "verb": r["verb"],
                        "object": r["object"],
                    })
        return out

    async def stamp_claims_status(self, conv_id: int, status: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET claims_status = $1 WHERE id = $2",
                status, int(conv_id),
            )

    async def fetch_pending_reception(self, limit: int = 50) -> list[dict]:
        """Spans orphaned between insert_turn and the reception stage (crash)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, session_id, role, content FROM conversations
                    WHERE claims_status = 'pending' ORDER BY id LIMIT $1""",
                int(limit),
            )
        return [dict(r) for r in rows]

    async def ensure_flower_labels(self) -> None:
        """Session / Turn / NEXT / IN_SESSION only. No Concept or ABOUT."""
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            try:
                async with conn.transaction():
                    for idx, (kind, label) in enumerate(
                        [
                            ("v", "Turn"),
                            ("v", "Session"),
                            ("e", "NEXT"),
                            ("e", "IN_SESSION"),
                        ]
                    ):
                        sp = savepoint_name(f"flower_{label.lower()}", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            if kind == "v":
                                await conn.execute(
                                    "SELECT create_vlabel($1, $2)", self.graph_name, label
                                )
                            else:
                                await conn.execute(
                                    "SELECT create_elabel($1, $2)", self.graph_name, label
                                )
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception as exc:
                            try:
                                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            except asyncpg.PostgresError:
                                pass
                            try:
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                            except asyncpg.PostgresError:
                                pass
                            if "already exists" not in str(exc).lower():
                                logger.warning("ensure flower label %s failed", label, exc_info=True)
            except Exception:
                logger.warning("ensure_flower_labels transaction error", exc_info=True)

    async def purge_verify_turns(self) -> int:
        """DETACH DELETE Turn vertices whose session_id is a C5 verify synthetic.

        Also drops orphan conversation bridges whose conv_* row is gone.
        """
        graph = self.graph_name
        deleted = 0
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            sp = savepoint_name("purge_vfy", 0)
            try:
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        _cy_body = "MATCH (t:Turn) WHERE t.session_id STARTS WITH 'verify-c5' DETACH DELETE t RETURN count(t) "
                        row = await conn.fetchrow(
                            f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (c agtype)"
                        )
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        if row is not None:
                            try:
                                deleted = int(str(row["c"]).strip().strip('"') or 0)
                            except ValueError:
                                deleted = 0
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        logger.debug("purge_verify_turns cypher failed", exc_info=True)
            except Exception:
                logger.debug("purge_verify_turns txn failed", exc_info=True)
            try:
                await conn.execute(
                    """
                    DELETE FROM memory_chunk_nodes b
                     WHERE b.source = 'conversation'
                       AND NOT EXISTS (
                         SELECT 1 FROM conversations c
                          WHERE b.chunk_id = 'conv_' || c.id::text
                       )
                    """
                )
            except Exception:
                logger.debug("purge verify bridges failed", exc_info=True)
        return deleted

    async def upsert_memory_entry(
        self,
        agent_identity: str,
        target: str,
        content: str,
        vec_literal: Optional[str],
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        assert_embedding_compatible(vec_literal, self.embed_dim)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_entries
                    (agent_identity, target, content, embedding, metadata,
                     embed_model, embed_dim)
                VALUES ($1, $2, $3, $4::vector, $5::jsonb, $6, $7)
                ON CONFLICT (agent_identity, target, md5(content)) DO NOTHING
                """,
                agent_identity,
                target,
                content,
                vec_literal,
                json.dumps(metadata or {}),
                self.embed_model,
                self.embed_dim,
            )

    async def replace_memory_entries(
        self,
        agent_identity: str,
        target: str,
        old_text: str,
        content: str,
        vec_literal: Optional[str],
        metadata: Dict[str, Any] | None = None,
    ) -> int:
        """Replace entries whose content contains ``old_text``; insert if none matched."""
        async with self.pool.acquire() as conn:
            n = await conn.execute(
                """
                UPDATE memory_entries
                   SET content = $1, embedding = $2::vector, updated_at = now()
                 WHERE agent_identity = $3 AND target = $4 AND content LIKE $5 ESCAPE '\'
                """,
                content,
                vec_literal,
                agent_identity,
                target,
                f"%{_escape_like(old_text)}%",
            )
            if n == "UPDATE 0":
                await conn.execute(
                    """
                    INSERT INTO memory_entries
                        (agent_identity, target, content, embedding, metadata,
                         embed_model, embed_dim)
                    VALUES ($1, $2, $3, $4::vector, $5::jsonb, $6, $7)
                    """,
                    agent_identity,
                    target,
                    content,
                    vec_literal,
                    json.dumps(metadata or {}),
                    self.embed_model,
                    self.embed_dim,
                )
                return 1
            return int(n.split()[1])

    async def remove_memory_entries(
        self, agent_identity: str, target: str, content_substr: str
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM memory_entries
                 WHERE agent_identity = $1 AND target = $2 AND content LIKE $3 ESCAPE '\'
                """,
                agent_identity,
                target,
                f"%{_escape_like(content_substr)}%",
            )

    async def librarian_health(self) -> Dict[str, int]:
        """Librarian-scoped health counts — file-backed rows only (issue #17).

        The naive query ``WHERE metadata->>'hash' IS NULL`` false-positives on
        user prefs written via the generic memory tool (no file_path, no hash
        by design — e.g. ids 1799-1801). Correct scope is file-backed only:

            WHERE metadata->>'file_path' IS NOT NULL
              AND (metadata->>'hash' IS NULL OR metadata->>'doc_type' IS NULL)

        Returns ``{"total_file_backed": N, "missing_hash": H, "missing_doc_type": D}``.
        Callers should assert H==0 and D==0; user prefs are excluded by design.
        """
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM memory_entries WHERE metadata->>'file_path' IS NOT NULL"
            )
            missing_hash = await conn.fetchval(
                "SELECT count(*) FROM memory_entries WHERE metadata->>'file_path' IS NOT NULL AND metadata->>'hash' IS NULL"
            )
            missing_doc = await conn.fetchval(
                "SELECT count(*) FROM memory_entries WHERE metadata->>'file_path' IS NOT NULL AND metadata->>'doc_type' IS NULL"
            )
            return {
                "total_file_backed": int(total or 0),
                "missing_hash": int(missing_hash or 0),
                "missing_doc_type": int(missing_doc or 0),
            }

    # -- Vector search ----------------------------------------------------------

    async def vector_search(self, vec_literal: str, k: int) -> List[Dict[str, Any]]:
        """ANN over file memory, doc_chunks, and conversation turns.

        File-backed ``memory_entries`` (metadata.file_path set) are omitted:
        ingest already stores those first-chunk embeddings in ``doc_chunks``,
        so including both would duplicate a slot at identical cosine.

        Legacy NULL ``embed_model``/``embed_dim`` (trust_nomic_768): unstamped
        rows stay in this candidate pool. They are not filtered and not
        backfilled. Do not treat NULL as the live config default if a second
        embed model is introduced.

        Each arm ``ORDER BY embedding <=> $1 LIMIT k`` so the HNSW indexes
        (memory_entries, doc_chunks, conversations) can serve ANN; the outer
        union then reranks the bounded candidate set.
        """
        sql = """
            SELECT id, content, embedding, similarity, src, session_id, ts FROM (
              (
                SELECT id::text AS id, content, embedding::text AS embedding,
                       1 - (embedding <=> $1::vector) AS similarity,
                       'memory_entry'::text AS src,
                       NULL::text AS session_id,
                       created_at AS ts
                  FROM memory_entries
                 WHERE embedding IS NOT NULL
                   AND metadata->>'file_path' IS NULL
                 ORDER BY embedding <=> $1::vector
                 LIMIT $2
              )
              UNION ALL
              (
                SELECT id AS id, content, embedding::text AS embedding,
                       1 - (embedding <=> $1::vector) AS similarity,
                       'doc_chunk'::text AS src,
                       NULL::text AS session_id,
                       created_at AS ts
                  FROM doc_chunks
                 WHERE embedding IS NOT NULL
                 ORDER BY embedding <=> $1::vector
                 LIMIT $2
              )
              UNION ALL
              (
                SELECT id::text AS id, content, embedding::text AS embedding,
                       1 - (embedding <=> $1::vector) AS similarity,
                       'conversation'::text AS src,
                       session_id,
                       ts
                  FROM conversations
                 WHERE embedding IS NOT NULL
                   AND coalesce(metadata->>'kind', 'interactive') = 'interactive'
                   AND coalesce(session_id, '') NOT LIKE 'verify-c5%'
                   AND coalesce(session_id, '') NOT LIKE 'bench-%'
                 ORDER BY embedding <=> $1::vector
                 LIMIT $2
              )
            ) u
            ORDER BY similarity DESC
            LIMIT $2
        """
        ef = clamp_hnsw_ef_search(self.hnsw_ef_search)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(hnsw_ef_search_sql(ef))
                rows = await conn.fetch(sql, vec_literal, k)
        out = [dict(r) for r in rows]
        logger.info("vector_search k=%s hnsw.ef_search=%s hits=%s", k, ef, len(out))
        return out

    async def bridge_vertex_ids(self, chunk_ids: Sequence[str]) -> List[str]:
        """Bridge-table lookup. Returns vertex ids STRINGIFIED (bigint precision).

        Canonical is memory_entries.id::text; legacy 'mem_' prefix and
        'conv_' alias are resolved as fallbacks (V8 migration window).
        """
        if not chunk_ids:
            return []
        expanded = []
        seen: set[str] = set()
        for cid in list(chunk_ids):
            cid = str(cid)
            if cid not in seen:
                expanded.append(cid)
                seen.add(cid)
            # canonical numeric -> legacy mem_ alias
            if cid.isdigit():
                alias = f"mem_{cid}"
                if alias not in seen:
                    expanded.append(alias)
                    seen.add(alias)
            elif cid.startswith("mem_") and cid[4:].isdigit():
                canon = cid[4:]
                if canon not in seen:
                    expanded.append(canon)
                    seen.add(canon)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT vertex_id::text AS vid
                FROM memory_chunk_nodes
                WHERE chunk_id = ANY($1::text[])
                """,
                expanded,
            )
        return [r["vid"] for r in rows]

    async def bridge_map(self, chunk_ids: Sequence[str]) -> Dict[str, List[str]]:
        """chunk_id -> vertex id strings (AGE bigint as text).

        Handles canonical / mem_ alias duality: a bridge row written as
        'mem_42' is also reachable via canonical '42' and vice-versa, so
        provider's bmap.get(i) never misses during the migration window.
        """
        if not chunk_ids:
            return {}
        # expand for SQL lookup (canonical <-> mem_ duality)
        expanded = []
        seen: set[str] = set()
        for cid in list(chunk_ids):
            cid = str(cid)
            if cid not in seen:
                expanded.append(cid)
                seen.add(cid)
            if cid.isdigit():
                alias = f"mem_{cid}"
                if alias not in seen:
                    expanded.append(alias)
                    seen.add(alias)
            elif cid.startswith("mem_") and cid[4:].isdigit():
                canon = cid[4:]
                if canon not in seen:
                    expanded.append(canon)
                    seen.add(canon)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT chunk_id, vertex_id::text AS vid
                  FROM memory_chunk_nodes
                 WHERE chunk_id = ANY($1::text[])
                """,
                expanded,
            )
        out: Dict[str, List[str]] = {}
        for r in rows:
            ck = str(r["chunk_id"])
            vid = str(r["vid"])
            out.setdefault(ck, []).append(vid)
            # alias back-fill so canonical lookup finds legacy rows
            if ck.startswith("mem_") and ck[4:].isdigit():
                canon = ck[4:]
                if vid not in out.get(canon, []):
                    out.setdefault(canon, []).append(vid)
            elif ck.isdigit():
                alias = f"mem_{ck}"
                if vid not in out.get(alias, []):
                    out.setdefault(alias, []).append(vid)
        # also ensure requested canonical keys exist even if only alias rows found
        # (already handled above, but dedupe)
        for k in list(out.keys()):
            out[k] = list(dict.fromkeys(out[k]))
        return out

    async def drop_graph(self, graph_name: str | None = None) -> None:
        """Drop an AGE graph safely (identifier-quoted, no f-string injection).

        Synchronous psycopg callers should prefer the SQL-composable form::

            conn.execute(SQL("SELECT drop_graph({})").format(Identifier(name)))

        This async variant validates the name against the safe-identifier pattern
        and double-quote escapes it before interpolating into the asyncpg query.
        """
        name = graph_name or self.graph_name
        check_label(name)
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            sp = _savepoint_name("drop_graph", 0)
            try:
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        # Use parameterized query to avoid f-string interpolation of graph name
                        await conn.execute("SELECT drop_graph($1)", name)
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        try:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        except Exception:
                            pass
                        try:
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception:
                            pass
                        raise
            except Exception:
                logger.warning("drop_graph failed for %r", name, exc_info=True)
                raise

    def drop_graph_sql(self, graph_name: str | None = None):
        """Return a psycopg SQL composable for dropping a graph (for sync callers).

        Usage:
            from psycopg.sql import Identifier, SQL
            conn.execute(store.drop_graph_sql("my_graph"))

        Falls back to a validated quoted string if psycopg is unavailable.
        """
        name = graph_name or self.graph_name
        check_label(name)
        if SQL is not None and Identifier is not None:
            return SQL("SELECT drop_graph({})").format(Identifier(name))
        return f"SELECT drop_graph({age_str(name)})"
