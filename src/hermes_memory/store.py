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
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import asyncpg

try:
    from psycopg.sql import Identifier, SQL
except ImportError:
    Identifier = None  # type: ignore[assignment]
    SQL = None  # type: ignore[assignment]

logger = logging.getLogger("hybrid_age.store")

# ---------------------------------------------------------------------------
# Cypher escaping helpers (property-test surface P1/P2)
# ---------------------------------------------------------------------------

def age_str(value: Any) -> str:
    """Escape a Python value as a Cypher string literal.

    Guarantees no unescaped ``'`` or ``\\`` survives from the input.
    Non-string values are str()-ed first; None becomes 'null' (unquoted).
    Also escapes ``$`` (``\\$``) so the value can never close a
    dollar-quoted ``cypher('graph', $$ body $$)`` wrapper.
    """
    if value is None:
        return "null"
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace("$", "\\$")
    s = s.replace("'", "\\'")
    # neutralize any other backslash-sensitive control chars
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f"'{s}'"


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


def _pick_cypher_dollar_tag(body: str) -> str:
    """Pick a ``$tag$`` that does not collide with ``body``.

    - If ``$$`` is absent, ``$$`` is safe (only ``$$`` would close it).
    - Otherwise scan ``$cy0$``, ``$cy1$``, ... for the first tag absent
      from ``body``.  This covers both ``$$`` and any ``$tag$`` collision.
    - Raise if no tag found (body adversarially contains all candidates).
    """
    if "$$" not in body:
        return "$$"
    for i in range(10000):
        tag = f"$cy{i}$"
        if tag not in body:
            return tag
    raise ValueError("cypher body contains too many colliding dollar-quote tags")


def cypher_dollar_quote(body: str) -> str:
    """Wrap ``body`` in a safe dollar-quote tag."""
    tag = _pick_cypher_dollar_tag(body)
    return f"{tag}{body}{tag}"


def cypher_call(graph: str, body: str) -> str:
    """Return ``cypher('graph', $tag$body$tag$)`` with safe quoting."""
    validate_graph_name(graph)
    return f"cypher('{graph}', {cypher_dollar_quote(body)})"


# Backwards-compatible aliases
_pick_dollar_tag = _pick_cypher_dollar_tag


def age_props(properties: Dict[str, Any]) -> str:
    """Render a dict as a Cypher property map.

    - None values are dropped entirely.
    - Keys must match the safe-identifier pattern (same as labels); invalid
      keys are skipped with a warning rather than interpolated into Cypher.
    - Numeric values (int/float) are emitted as bare literals so Cypher
      numeric comparisons (coalesce(r.weight,0.5) >= $min) work; all other
      values go through age_str (quoted).
    - Keys are preserved exactly once, insertion order kept.
    """
    parts = []
    for key, val in properties.items():
        if val is None:
            continue
        if not _SAFE_IDENT.match(key or ""):
            logger.warning("age_props: skipping invalid property key %r", key)
            continue
        if isinstance(val, bool):
            parts.append(f"{key}: {str(val).lower()}")
        elif isinstance(val, (int, float)):
            parts.append(f"{key}: {val}")
        else:
            parts.append(f"{key}: {age_str(val)}")
    return "{" + ", ".join(parts) + "}"


_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_label(label: str) -> str:
    """Validate a Cypher label against the safe-identifier pattern.

    Public API: raises ValueError on invalid labels. ``_check_label`` is kept
    as a backwards-compatible alias.
    """
    if not _SAFE_IDENT.match(label or ""):
        raise ValueError(f"invalid AGE label: {label!r}")
    return label


def validate_graph_name(graph: str) -> str:
    """Validate a graph name for safe interpolation into cypher('...', $$...$$).

    Public API. Raises ValueError on names outside [_SAFE_IDENT]. Called once
    at Store construction; from then on the stored value is trusted.
    """
    if not _SAFE_IDENT.match(graph or ""):
        raise ValueError(f"invalid AGE graph name: {graph!r}")
    return graph


_check_label = check_label  # backwards-compatible alias


def _escape_like(text: str) -> str:
    r"""Escape LIKE wildcards (\ % _) for use with ESCAPE '\'."""
    return (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def savepoint_name(prefix: str, idx: int) -> str:
    """Build a SQL savepoint identifier (public API).

    ``_savepoint_name`` is kept as a backwards-compatible alias.
    """
    return f"sp_{prefix}_{idx}"


_savepoint_name = savepoint_name  # backwards-compatible alias


def dedup_key(name: str, label: str) -> Tuple[str, str]:
    """Canonical vertex identity for MERGE/backfill dedup (public API).

    Entities merge on lowercased, whitespace-trimmed name within their label.
    This is the single derivation used by backfill dedup; the property suite
    in tests/property/test_p8_backfill_dedup.py pins it against this function
    rather than a local copy.
    """
    return (name.strip().lower(), label)

def _cosine_similarity(a, b):
    """Pure-python cosine: dot/(||a||*||b||). None if invalid."""
    if not a or not b or len(a) != len(b):
        return None
    try:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return None
        return dot / (na * nb)
    except Exception:
        return None


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

def recency_score(
    weight: float,
    cosine: float,
    created_at: Optional[str] = None,
    half_life_days: float = 30.0,
    now: Optional[datetime.datetime] = None,
    *,
    edge_created_at: Optional[str] = None,
    vertex_created_at: Optional[str] = None,
) -> float:
    """Composite score: 0.5*cosine + 0.3*weight + 0.2*exp(-age/half_life).

    created_at is shorthand for edge_created_at when vertex not needed.
    Weight and cosine fallback to 0.5 if None/nan.
    """
    try:
        w = float(weight) if weight is not None else 0.5
    except Exception:
        w = 0.5
    try:
        c = float(cosine) if cosine is not None else 0.5
    except Exception:
        c = 0.5
    # sanitize 0-1 range? keep as given but clamp nan
    if w != w:  # nan
        w = 0.5
    if c != c:
        c = 0.5
    ea = edge_created_at if edge_created_at is not None else created_at
    va = vertex_created_at
    decay = recency_decay_for_edge(ea, va, half_life_days, now)
    return 0.5 * c + 0.3 * w + 0.2 * decay


class Store:
    """Async data access over the pgvector + AGE schema."""

    def __init__(self, pool: asyncpg.Pool, graph_name: str = "hermes_knowledge"):
        # Validated once here; the f-string cypher wrappers in this module
        # interpolate ONLY this vetted value (issue #10).
        self.graph_name = validate_graph_name(graph_name)
        self.pool = pool

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
        """Insert a turn and return its id (RETURNING id) for graph linkage."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations
                    (session_id, agent_identity, role, content, embedding, metadata)
                VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                RETURNING id
                """,
                session_id,
                agent_identity,
                role,
                content,
                vec_literal,
                json.dumps(metadata or {}),
            )
            return int(row["id"]) if row and row["id"] is not None else None

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
                            except Exception:
                                pass
                            try:
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                            except Exception:
                                pass
                            if "already exists" not in str(exc).lower():
                                logger.debug("ensure label %s failed", label, exc_info=True)
            except Exception:
                logger.debug("ensure_about_labels transaction error", exc_info=True)

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
                logger.debug("bridge_turn failed", exc_info=True)

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
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_entries
                    (agent_identity, target, content, embedding, metadata)
                VALUES ($1, $2, $3, $4::vector, $5::jsonb)
                ON CONFLICT (agent_identity, target, md5(content)) DO NOTHING
                """,
                agent_identity,
                target,
                content,
                vec_literal,
                json.dumps(metadata or {}),
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
                        (agent_identity, target, content, embedding, metadata)
                    VALUES ($1, $2, $3, $4::vector, $5::jsonb)
                    """,
                    agent_identity,
                    target,
                    content,
                    vec_literal,
                    json.dumps(metadata or {}),
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

        Each arm ``ORDER BY embedding <=> $1 LIMIT k`` so the HNSW indexes
        (memory_entries, doc_chunks, conversations) can serve ANN; the outer
        union then reranks the bounded candidate set.
        """
        sql = """
            SELECT id, content, similarity, src FROM (
              (
                SELECT id::text AS id, content,
                       1 - (embedding <=> $1::vector) AS similarity,
                       'memory_entry'::text AS src
                  FROM memory_entries
                 WHERE embedding IS NOT NULL
                   AND metadata->>'file_path' IS NULL
                 ORDER BY embedding <=> $1::vector
                 LIMIT $2
              )
              UNION ALL
              (
                SELECT id AS id, content,
                       1 - (embedding <=> $1::vector) AS similarity,
                       'doc_chunk'::text AS src
                  FROM doc_chunks
                 WHERE embedding IS NOT NULL
                 ORDER BY embedding <=> $1::vector
                 LIMIT $2
              )
              UNION ALL
              (
                SELECT id::text AS id, content,
                       1 - (embedding <=> $1::vector) AS similarity,
                       'conversation'::text AS src
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
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, vec_literal, k)
        return [dict(r) for r in rows]

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
                expanded.append(cid); seen.add(cid)
            # canonical numeric -> legacy mem_ alias
            if cid.isdigit():
                alias = f"mem_{cid}"
                if alias not in seen:
                    expanded.append(alias); seen.add(alias)
            elif cid.startswith("mem_") and cid[4:].isdigit():
                canon = cid[4:]
                if canon not in seen:
                    expanded.append(canon); seen.add(canon)
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
                expanded.append(cid); seen.add(cid)
            if cid.isdigit():
                alias = f"mem_{cid}"
                if alias not in seen:
                    expanded.append(alias); seen.add(alias)
            elif cid.startswith("mem_") and cid[4:].isdigit():
                canon = cid[4:]
                if canon not in seen:
                    expanded.append(canon); seen.add(canon)
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

    # -- Graph expansion (SAVEPOINT-wrapped Cypher) -----------------------------

    async def expand_graph(
        self, seed_vertex_ids: Sequence[int], rel_types: Sequence[str] = (), limit: int = 40,
        min_weight: float = 0.0, min_cosine: float = 0.0,
        decay_half_life_days: float = 30.0,
    ) -> List[Tuple[Any, Optional[str], Any, float, float, float, float]]:
        """1-hop weighted expand from seed vertices. Cypher inside SAVEPOINT.

        Returns 7-tuples ``(n, rel, m, weight, cosine, decay, score)``.
        Search packing appends hop as an 8th element; consumers must read hop
        from the last field, not index 5 (that is decay).

        Dynamic memory graph: edges carry weight (force) and cosine (vector radius).
        Recency-aware scoring: 0.5*cosine + 0.3*weight + 0.2*exp(-age_days/half_life)
        where age from edge created_at or target vertex created_at, fallback 0.5
        for legacy edges without timestamps. If rel_types empty → walk all types
        scored with decay. Otherwise filter to that whitelist but still
        score-ordered. Edges without props default to 0.5 so legacy IMPORTS
        edges remain walkable.
        """
        if not seed_vertex_ids:
            return []
        ids_literal = "[" + ", ".join(str(int(i)) for i in seed_vertex_ids) + "]"
        graph = self.graph_name
        if rel_types:
            rels = ", ".join(age_str(r) for r in rel_types)
            type_filter = f"type(r) IN [{rels}] AND "
        else:
            type_filter = ""
        # Fetch extra columns for recency scoring; Python re-ranks with decay.
        # Fetch 2x limit to allow newer low-w*c edges to surface via recency.
        fetch_limit = max(int(limit) * 2, int(limit) + 20) if int(limit) > 0 else 0
        _cy_body = f"""
                MATCH (n)
                WHERE id(n) IN {ids_literal}
                OPTIONAL MATCH (n)-[r]->(m)
                WHERE {type_filter}coalesce(r.weight, 0.5) >= {float(min_weight)}
                  AND coalesce(r.cosine, 0.5) >= {float(min_cosine)}
                RETURN n, type(r) AS rel, m, coalesce(r.weight, 0.5), coalesce(r.cosine, 0.5), r.created_at, m.created_at
                LIMIT {int(fetch_limit)}
            """
        cypher = f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (n agtype, rel agtype, m agtype, w agtype, c agtype, r_created agtype, m_created agtype)"
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            sp = _savepoint_name("expand", 0)
            try:
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        rows = await conn.fetch(cypher)
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        logger.warning("cypher expand failed; rolled to savepoint", exc_info=True)
                        return []
                # Recency-aware re-ranking in Python
                scored: List[Tuple[float, Any]] = []
                for r in rows:
                    # Parse weight/cosine agtype -> float with fallback 0.5
                    try:
                        raw_w = r["w"]
                        w = float(str(raw_w).strip('"')) if raw_w is not None and str(raw_w).strip('"') != "null" else 0.5
                    except Exception:
                        w = 0.5
                    try:
                        raw_c = r["c"]
                        c = float(str(raw_c).strip('"')) if raw_c is not None and str(raw_c).strip('"') != "null" else 0.5
                    except Exception:
                        c = 0.5
                    # Edge/target created_at for decay (fallback 0.5 already in helper)
                    edge_ca = _parse_created_at(r["r_created"]) if "r_created" in r else None
                    # r may not have key if old cypher fallback? handle gracefully
                    # Use helper that already normalizes; but pass raw string iso
                    # r_created,m_created may be agtype None/null
                    vert_ca = _parse_created_at(r["m_created"]) if "m_created" in r else None
                    # Compute decay and composite score
                    decay = recency_decay_for_edge(edge_ca, vert_ca, decay_half_life_days)
                    score = 0.5 * c + 0.3 * w + 0.2 * decay
                    scored.append((score, r))
                # Sort by score descending, stable tie-break preserves fetch order
                scored.sort(key=lambda x: x[0], reverse=True)
                # Trim to requested limit
                trimmed = scored[: int(limit)] if int(limit) > 0 else scored
                out: List[Tuple[Any, Optional[str], Any, float, float, float, float]] = []
                for _score, row in trimmed:
                    try:
                        raw_w = row["w"]
                        w = float(str(raw_w).strip('"')) if raw_w is not None and str(raw_w).strip('"') != "null" else 0.5
                    except Exception:
                        w = 0.5
                    try:
                        raw_c = row["c"]
                        c = float(str(raw_c).strip('"')) if raw_c is not None and str(raw_c).strip('"') != "null" else 0.5
                    except Exception:
                        c = 0.5
                    # decay/score already computed in scored; recompute for return
                    edge_ca = _parse_created_at(row["r_created"]) if "r_created" in row else None
                    vert_ca = _parse_created_at(row["m_created"]) if "m_created" in row else None
                    decay = recency_decay_for_edge(edge_ca, vert_ca, decay_half_life_days)
                    score = 0.5 * c + 0.3 * w + 0.2 * decay
                    out.append((row["n"], row["rel"], row["m"], w, c, decay, score))
                return out
            except Exception:
                logger.debug("expand_graph transaction error", exc_info=True)
                return []

    # -- Batched MERGE (>=50 statements per transaction) ------------------------

    async def merge_vertices_batched(
        self, items: Iterable[Tuple[str, Dict[str, Any]]], batch_size: int = 50
    ) -> List[Optional[str]]:
        """MERGE vertices on minimal unique key {name}; batch >=50 per txn.

        Each statement is SAVEPOINT-guarded so one failure never poisons the
        batch's transaction. Returns STRINGIFIED vertex ids (None on failure).
        """
        results: List[Optional[str]] = []
        items = list(items)
        graph = self.graph_name

        for start in range(0, len(items), batch_size):
            chunk_items = items[start : start + batch_size]
            async with self.pool.acquire() as conn:
                await self.load_age(conn)
                try:
                    async with conn.transaction():
                        for idx, (label, props) in enumerate(chunk_items):
                            sp = _savepoint_name("merge_v", idx)
                            await conn.execute(f"SAVEPOINT {sp}")
                            try:
                                name = props.get("name") or props.get("path")
                                if not name:
                                    results.append(None)
                                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                    continue
                                key_prop = "path" if "path" in props else "name"
                                non_key = {k: v for k, v in props.items() if k != key_prop}
                                # AGE 1.6 has no ON CREATE SET / ON MATCH SET;
                                # MERGE on the minimal key, then SET only mutable
                                # props. For Concept weight/created_at we use
                                # ON-CREATE semantics (coalesce) so accumulated
                                # weight and original created_at are not reset.
                                if not non_key:
                                    cypher = (
                                        f"MERGE (v:{_check_label(label)} {{{key_prop}: {age_str(name)}}}) RETURN id(v)"
                                    )
                                else:
                                    set_parts = []
                                    for k, v in non_key.items():
                                        if k in ("weight", "created_at"):
                                            # preserve existing value if present
                                            if isinstance(v, bool):
                                                lit = str(v).lower()
                                            elif isinstance(v, (int, float)):
                                                lit = str(v)
                                            else:
                                                lit = age_str(v)
                                            set_parts.append(f"v.{k} = coalesce(v.{k}, {lit})")
                                        else:
                                            if isinstance(v, bool):
                                                lit = str(v).lower()
                                            elif isinstance(v, (int, float)):
                                                lit = str(v)
                                            else:
                                                lit = age_str(v)
                                            set_parts.append(f"v.{k} = {lit}")
                                    set_clause = ", ".join(set_parts)
                                    cypher = (
                                        f"MERGE (v:{_check_label(label)} {{{key_prop}: {age_str(name)}}})\n"
                                        f"SET {set_clause}\n"
                                        f"RETURN id(v)"
                                    )
                                row = await conn.fetchrow(
                                    f"SELECT * FROM {cypher_call(graph, cypher)} AS (id agtype)"
                                )
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                if row is not None:
                                    raw = row["id"]
                                    vid = str(raw).strip('"')
                                    try:
                                        results.append(str(int(vid)))  # stringify at boundary
                                    except ValueError:
                                        results.append(None)
                                else:
                                    results.append(None)
                            except Exception:
                                try:
                                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                                except Exception:
                                    pass
                                try:
                                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                except Exception:
                                    pass
                                logger.debug("vertex MERGE failed %s", props, exc_info=True)
                                results.append(None)
                except Exception:
                    logger.warning("batch txn failed wholesale", exc_info=True)
                    # Pad only the remainder of THIS chunk (items before the txn's
                    # first failure may already have appended results).
                    expected = start + len(chunk_items)
                    if len(results) < expected:
                        results.extend([None] * (expected - len(results)))
        return results

    async def merge_edges_batched(
        self, edges: Iterable[Tuple[str, int, int]], batch_size: int = 50,
        edge_props: Optional[Dict[Tuple[str,int,int], Dict[str, Any]]] = None,
    ) -> int:
        """MERGE edges on (start, end, label); SAVEPOINT-guarded, batched.

        edge_props optional map (label,src,dst) -> {weight, cosine, ...}
        merged via SET e += props (AGE 1.6 MERGE then SET).
        """
        done = 0
        edges = list(edges)
        graph = self.graph_name
        for start in range(0, len(edges), batch_size):
            chunk_edges = edges[start : start + batch_size]
            async with self.pool.acquire() as conn:
                await self.load_age(conn)
                try:
                    async with conn.transaction():
                        for idx, (label, src, dst) in enumerate(chunk_edges):
                            sp = _savepoint_name("merge_e", idx)
                            await conn.execute(f"SAVEPOINT {sp}")
                            try:
                                props = (edge_props or {}).get((label, src, dst), {})
                                props_str = f" SET e += {age_props(props)}" if props else ""
                                cypher = (
                                    f"MATCH (a), (b) WHERE id(a) = {int(src)} AND id(b) = {int(dst)} "
                                    f"MERGE (a)-[e:{_check_label(label)}]->(b){props_str} RETURN id(e)"
                                )
                                row = await conn.fetchrow(
                                    f"SELECT * FROM {cypher_call(graph, cypher)} AS (id agtype)"
                                )
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                if row is not None:
                                    done += 1
                            except Exception:
                                try:
                                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                                except Exception:
                                    pass
                                try:
                                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                except Exception:
                                    pass
                                logger.debug("edge MERGE failed", exc_info=True)
                except Exception:
                    logger.warning("edge batch txn failed", exc_info=True)
        return done


    # -- Concept compaction helpers (issue #37) ---------------------------------

    async def fetch_concepts(self):
        """Fetch all Concept vertices: id, name, weight, created_at, degree.

        Returns list of dicts {id:int, name:str, weight:int, created_at:str|None, degree:int}.
        SAVEPOINT-guarded; Concept vlabel ensured via ensure_about_labels.
        """
        await self.ensure_about_labels()
        graph = self.graph_name
        _cy_body = "MATCH (c:Concept) OPTIONAL MATCH (c)-[r]-() WITH c, count(r) AS degree RETURN id(c), c.name, c.weight, c.created_at, degree "
        cypher = f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (id agtype, name agtype, weight agtype, created_at agtype, degree agtype)"
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            sp = savepoint_name("fetch_concepts", 0)
            rows = []
            try:
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        rows = await conn.fetch(cypher)
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        logger.debug("fetch_concepts failed", exc_info=True)
                        return []
            except Exception:
                logger.debug("fetch_concepts txn failed", exc_info=True)
                return []
        out = []
        for r in rows:
            try:
                raw_id = r["id"]
                vid = int(str(raw_id).strip('"'))
                raw_name = r["name"]
                name = str(raw_name).strip('"') if raw_name is not None else ""
                if name == "null":
                    name = ""
                raw_w = r["weight"]
                weight = 1
                if raw_w is not None:
                    s = str(raw_w).strip('"')
                    if s != "null" and s != "":
                        try:
                            weight = int(float(s))
                        except Exception:
                            weight = 1
                raw_ca = r["created_at"]
                ca = None
                if raw_ca is not None:
                    s = str(raw_ca).strip('"')
                    if s != "null" and s != "":
                        ca = s
                raw_deg = r["degree"]
                degree = 0
                if raw_deg is not None:
                    try:
                        degree = int(str(raw_deg).strip('"'))
                    except Exception:
                        degree = 0
                out.append({"id": vid, "name": name, "weight": weight, "created_at": ca, "degree": degree})
            except Exception:
                continue
        return out

    async def fetch_concept_names(self) -> list[str]:
        """Names only — no degree walk. Used as ABOUT hub candidates."""
        graph = self.graph_name
        _cy_body = "MATCH (c:Concept) RETURN c.name "
        cypher = f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (name agtype)"
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            sp = savepoint_name("fetch_cnames", 0)
            try:
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        rows = await conn.fetch(cypher)
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        return []
            except Exception:
                return []
        names: list[str] = []
        for r in rows:
            raw = r["name"]
            name = str(raw).strip('"') if raw is not None else ""
            if name and name != "null":
                names.append(name)
        return names

    async def fetch_concept_id_names(self) -> list[tuple[int, str]]:
        """id + name for Concept verts. No degree walk."""
        graph = self.graph_name
        _cy_body = "MATCH (c:Concept) RETURN id(c), c.name "
        cypher = f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (id agtype, name agtype)"
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            sp = savepoint_name("fetch_cids", 0)
            try:
                async with conn.transaction():
                    await conn.execute(f"SAVEPOINT {sp}")
                    try:
                        rows = await conn.fetch(cypher)
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        return []
            except Exception:
                return []
        out: list[tuple[int, str]] = []
        for r in rows:
            try:
                vid = int(str(r["id"]).strip('"'))
            except Exception:
                continue
            raw = r["name"]
            name = str(raw).strip('"') if raw is not None else ""
            if name == "null":
                name = ""
            out.append((vid, name))
        return out

    async def purge_concept_ids(self, vids: Sequence[int]) -> int:
        """DETACH DELETE Concept vertices by AGE id. SAVEPOINT per delete."""
        if not vids:
            return 0
        graph = self.graph_name
        deleted = 0
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            try:
                async with conn.transaction():
                    for i, vid in enumerate(vids):
                        sp = savepoint_name("purge_c", i)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            _cy_body = f"MATCH (c:Concept) WHERE id(c) = {int(vid)} DETACH DELETE c RETURN 1 "
                            await conn.execute(
                                f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (ok agtype)"
                            )
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                            deleted += 1
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except Exception:
                logger.debug("purge_concept_ids txn failed", exc_info=True)
        return deleted

    async def find_orphan_concept_ids(self, days: int = 7):
        """Prune candidates: Concept degree==0 and older than days (created_at).

        If created_at missing, node is not considered orphan (conservative).
        """
        import datetime
        concepts = await self.fetch_concepts()
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        orphans = []
        for c in concepts:
            if c.get("degree", 0) != 0:
                continue
            ca = c.get("created_at")
            if not ca:
                continue
            try:
                iso = ca.replace("Z", "+00:00") if isinstance(ca, str) else ca
                dt = datetime.datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                if dt < cutoff:
                    orphans.append(int(c["id"]))
            except Exception:
                continue
        return orphans

    async def merge_concept_pair(self, keeper_id: int, loser_id: int):
        """SAVEPOINT-guarded merge of a near-duplicate Concept pair.

        - Ensures Concept vlabel exists (no ad-hoc labels).
        - Rewires all incident edges from loser -> keeper (MERGE + SET props).
        - Updates keeper weight as sum (numeric bare literal via age_props).
        - Moves bridge rows vertex_id loser->keeper.
        - DETACH DELETE loser.
        Returns True on success.
        """
        if int(keeper_id) == int(loser_id):
            return False
        await self.ensure_about_labels()
        graph = self.graph_name
        keeper_id = int(keeper_id)
        loser_id = int(loser_id)
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            try:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", loser_id)
                    sp_fetch = savepoint_name("merge_fetch", 0)
                    await conn.execute(f"SAVEPOINT {sp_fetch}")
                    try:
                        edge_rows = await conn.fetch(
                            f"SELECT * FROM {cypher_call(graph, f'MATCH (loser:Concept) WHERE id(loser) = {loser_id} MATCH (loser)-[r]->(m) RETURN type(r), id(m), r.weight, r.cosine ')} AS (t agtype, mid agtype, w agtype, c agtype)"
                        )
                        in_rows = await conn.fetch(
                            f"SELECT * FROM {cypher_call(graph, f'MATCH (loser:Concept) WHERE id(loser) = {loser_id} MATCH (n)-[r]->(loser) RETURN type(r), id(n), r.weight, r.cosine ')} AS (t agtype, nid agtype, w agtype, c agtype)"
                        )
                        wrow = await conn.fetchrow(
                            f"SELECT * FROM {cypher_call(graph, f'MATCH (k:Concept) WHERE id(k) = {keeper_id} RETURN k.weight ')} AS (w agtype)"
                        )
                        lrow = await conn.fetchrow(
                            f"SELECT * FROM {cypher_call(graph, f'MATCH (l:Concept) WHERE id(l) = {loser_id} RETURN l.weight ')} AS (w agtype)"
                        )
                        await conn.execute(f"RELEASE SAVEPOINT {sp_fetch}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_fetch}")
                        logger.debug("merge_concept_pair fetch failed", exc_info=True)
                        return False

                    def _parse_weight(raw):
                        if raw is None or str(raw).strip('"') == "null":
                            return 1
                        try:
                            s = str(raw).strip('"')
                            return int(float(s)) if s else 1
                        except Exception:
                            return 1

                    keeper_w = _parse_weight(wrow["w"] if wrow else None)
                    loser_w = _parse_weight(lrow["w"] if lrow else None)
                    new_weight = int(keeper_w + loser_w)

                    for idx, er in enumerate(edge_rows):
                        sp = savepoint_name("merge_out", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            label = str(er["t"]).strip('"')
                            check_label(label)
                            mid = int(str(er["mid"]).strip('"'))
                            props = {}
                            rw = er["w"]
                            rc = er["c"]
                            if rw is not None and str(rw).strip('"') != "null":
                                try:
                                    props["weight"] = float(str(rw).strip('"'))
                                except Exception:
                                    pass
                            if rc is not None and str(rc).strip('"') != "null":
                                try:
                                    props["cosine"] = float(str(rc).strip('"'))
                                except Exception:
                                    pass
                            set_parts = []
                            if "weight" in props:
                                set_parts.append(f"e.weight = CASE WHEN e.weight IS NULL OR e.weight < {float(props['weight'])} THEN {float(props['weight'])} ELSE e.weight END")
                            if "cosine" in props:
                                set_parts.append(f"e.cosine = CASE WHEN e.cosine IS NULL OR e.cosine < {float(props['cosine'])} THEN {float(props['cosine'])} ELSE e.cosine END")
                            props_str = (" SET " + ", ".join(set_parts)) if set_parts else ""
                            cypher = (
                                f"MATCH (a), (b) WHERE id(a) = {keeper_id} AND id(b) = {mid} "
                                f"MERGE (a)-[e:{label}]->(b){props_str} RETURN id(e)"
                            )
                            await conn.fetchrow(f"SELECT * FROM {cypher_call(graph, cypher)} AS (id agtype)")
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            logger.debug("merge outgoing edge failed", exc_info=True)
                            raise
                    for idx, er in enumerate(in_rows):
                        sp = savepoint_name("merge_in", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            label = str(er["t"]).strip('"')
                            check_label(label)
                            nid = int(str(er["nid"]).strip('"'))
                            props = {}
                            rw = er["w"]
                            rc = er["c"]
                            if rw is not None and str(rw).strip('"') != "null":
                                try:
                                    props["weight"] = float(str(rw).strip('"'))
                                except Exception:
                                    pass
                            if rc is not None and str(rc).strip('"') != "null":
                                try:
                                    props["cosine"] = float(str(rc).strip('"'))
                                except Exception:
                                    pass
                            set_parts = []
                            if "weight" in props:
                                set_parts.append(f"e.weight = CASE WHEN e.weight IS NULL OR e.weight < {float(props['weight'])} THEN {float(props['weight'])} ELSE e.weight END")
                            if "cosine" in props:
                                set_parts.append(f"e.cosine = CASE WHEN e.cosine IS NULL OR e.cosine < {float(props['cosine'])} THEN {float(props['cosine'])} ELSE e.cosine END")
                            props_str = (" SET " + ", ".join(set_parts)) if set_parts else ""
                            cypher = (
                                f"MATCH (a), (b) WHERE id(a) = {nid} AND id(b) = {keeper_id} "
                                f"MERGE (a)-[e:{label}]->(b){props_str} RETURN id(e)"
                            )
                            await conn.fetchrow(f"SELECT * FROM {cypher_call(graph, cypher)} AS (id agtype)")
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            logger.debug("merge incoming edge failed", exc_info=True)
                            raise

                    sp_w = savepoint_name("merge_weight", 0)
                    await conn.execute(f"SAVEPOINT {sp_w}")
                    try:
                        cypher = f"MATCH (k:Concept) WHERE id(k) = {keeper_id} SET k += {age_props({'weight': new_weight})} RETURN id(k)"
                        await conn.fetchrow(f"SELECT * FROM {cypher_call(graph, cypher)} AS (id agtype)")
                        await conn.execute(f"RELEASE SAVEPOINT {sp_w}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_w}")
                        logger.debug("merge weight update failed", exc_info=True)
                        raise

                    sp_bridge = savepoint_name("merge_bridge", 0)
                    await conn.execute(f"SAVEPOINT {sp_bridge}")
                    try:
                        await conn.execute(
                            "DELETE FROM memory_chunk_nodes WHERE graph_name = $1 AND vertex_id = $2 "
                            "AND (chunk_id, source) IN (SELECT chunk_id, source FROM memory_chunk_nodes WHERE vertex_id = $3 AND graph_name = $1)",
                            self.graph_name, keeper_id, loser_id,
                        )
                        await conn.execute(
                            "UPDATE memory_chunk_nodes SET vertex_id = $1 WHERE vertex_id = $2 AND graph_name = $3",
                            keeper_id, loser_id, self.graph_name,
                        )
                        await conn.execute(
                            "DELETE FROM memory_chunk_nodes a USING memory_chunk_nodes b "
                            "WHERE a.ctid < b.ctid AND a.chunk_id=b.chunk_id AND a.source=b.source AND a.vertex_id=b.vertex_id AND a.graph_name=b.graph_name"
                        )
                        await conn.execute(f"RELEASE SAVEPOINT {sp_bridge}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_bridge}")
                        logger.debug("bridge merge failed", exc_info=True)
                        raise

                    sp_del = savepoint_name("merge_delete", 0)
                    await conn.execute(f"SAVEPOINT {sp_del}")
                    try:
                        await conn.fetch(
                            f"SELECT * FROM {cypher_call(graph, f'MATCH (c:Concept) WHERE id(c) = {loser_id} DETACH DELETE c ')} AS (a agtype)"
                        )
                        await conn.execute(f"RELEASE SAVEPOINT {sp_del}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_del}")
                        logger.debug("loser DETACH DELETE failed", exc_info=True)
                        return False
            except Exception:
                logger.debug("merge_concept_pair transaction failed", exc_info=True)
                return False
        return True

    async def prune_orphan_concepts(self, orphan_ids):
        """SAVEPOINT-guarded DETACH DELETE for orphan Concept vertices + bridge cleanup.

        Revalidates degree==0 and age cutoff inside the same transaction (no new edges).
        Bridge cleanup is graph_name-scoped. Each delete in its own SAVEPOINT.
        Returns count of pruned vertices.
        """
        if not orphan_ids:
            return 0
        await self.ensure_about_labels()
        graph = self.graph_name
        import datetime as _dt
        cutoff_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).isoformat()
        pruned = 0
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            try:
                async with conn.transaction():
                    for idx, oid in enumerate(orphan_ids):
                        oid = int(oid)
                        await conn.execute("SELECT pg_advisory_xact_lock($1)", oid)
                        sp = savepoint_name("prune_orphan", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            # Revalidate: degree==0 and created_at older than cutoff, locked
                            _cy_body = f"MATCH (c:Concept) WHERE id(c) = {oid} OPTIONAL MATCH (c)-[r]-() WITH c, count(r) AS degree WHERE degree = 0 AND c.created_at IS NOT NULL AND c.created_at < {age_str(cutoff_iso)} RETURN id(c) "
                            rows = await conn.fetch(
                                f"SELECT * FROM {cypher_call(graph, _cy_body)} AS (id agtype)"
                            )
                            if not rows:
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                continue
                            await conn.fetch(
                                f"SELECT * FROM {cypher_call(graph, f'MATCH (c:Concept) WHERE id(c) = {oid} DETACH DELETE c ')} AS (a agtype)"
                            )
                            await conn.execute("DELETE FROM memory_chunk_nodes WHERE vertex_id = $1 AND graph_name = $2", oid, self.graph_name)
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                            pruned += 1
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            logger.debug("prune orphan %s failed", oid, exc_info=True)
            except Exception:
                logger.debug("prune_orphan_concepts txn failed", exc_info=True)
        return pruned

    async def preview_concept_compaction(self, pairs, orphan_ids):
        """Dry-run preview: pairs, orphans, bridge rows affected (no writes)."""
        bridge_affected = 0
        if pairs or orphan_ids:
            all_loser_ids = [int(l) for _, l, _ in pairs] + [int(o) for o in orphan_ids]
            if all_loser_ids:
                async with self.pool.acquire() as conn:
                    try:
                        bridge_affected = await conn.fetchval(
                            "SELECT count(*) FROM memory_chunk_nodes WHERE vertex_id = ANY($1::bigint[]) AND graph_name = $2",
                            all_loser_ids, self.graph_name,
                        )
                        bridge_affected = int(bridge_affected or 0)
                    except Exception:
                        bridge_affected = 0
        return {
            "pairs": [{"keeper": int(k), "loser": int(l), "cosine": float(c)} for k, l, c in pairs],
            "orphans": [int(o) for o in orphan_ids],
            "bridge_rows_affected": int(bridge_affected),
        }

    # -- Graph admin — injection-safe via identifier quoting ------------------
    # -- Graph admin — injection-safe via identifier quoting ------------------

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
