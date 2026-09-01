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

import json
import logging
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
    """
    if value is None:
        return "null"
    s = str(value)
    s = s.replace("\\", "\\\\").replace("'", "\\'")
    # neutralize any other backslash-sensitive control chars
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f"'{s}'"


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
                        [("v", "Turn"), ("v", "Concept"), ("e", "ABOUT")]
                    ):
                        sp = savepoint_name(f"ensure_{label.lower()}", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            if kind == "v":
                                await conn.execute(
                                    f"SELECT create_vlabel('{self.graph_name}', '{label}')"
                                )
                            else:
                                await conn.execute(
                                    f"SELECT create_elabel('{self.graph_name}', '{label}')"
                                )
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception as exc:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
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
        sql_entries = """
            SELECT id::text AS id, content,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memory_entries
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """
        sql_conversations = """
            SELECT id::text AS id, content,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM conversations
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql_entries, vec_literal, k)
            if not rows:
                rows = await conn.fetch(sql_conversations, vec_literal, k)
        return [dict(r) for r in rows]

    async def bridge_vertex_ids(self, chunk_ids: Sequence[str]) -> List[str]:
        """Bridge-table lookup. Returns vertex ids STRINGIFIED (bigint precision)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT vertex_id::text AS vid
                FROM memory_chunk_nodes
                WHERE chunk_id = ANY($1::text[])
                """,
                list(chunk_ids),
            )
        return [r["vid"] for r in rows]

    # -- Graph expansion (SAVEPOINT-wrapped Cypher) -----------------------------

    async def expand_graph(
        self, seed_vertex_ids: Sequence[int], rel_types: Sequence[str] = (), limit: int = 40,
        min_weight: float = 0.0, min_cosine: float = 0.0,
    ) -> List[Tuple[Any, Optional[str], Any]]:
        """1-hop weighted expand from seed vertices. Cypher inside SAVEPOINT.

        Dynamic memory graph: edges carry weight (force) and cosine (vector radius).
        If rel_types empty → walk all types scored by weight*cosine. Otherwise
        filter to that whitelist but still score-ordered. Edges without props
        default to 0.5 so legacy IMPORTS edges remain walkable.
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
        cypher = f"""
            SELECT * FROM cypher('{graph}', $$
                MATCH (n)
                WHERE id(n) IN {ids_literal}
                OPTIONAL MATCH (n)-[r]->(m)
                WHERE {type_filter}coalesce(r.weight, 0.5) >= {float(min_weight)}
                  AND coalesce(r.cosine, 0.5) >= {float(min_cosine)}
                RETURN n, type(r) AS rel, m
                ORDER BY coalesce(r.weight, 0.5) * coalesce(r.cosine, 0.5) DESC
                LIMIT {int(limit)}
            $$) AS (n agtype, rel agtype, m agtype)
        """
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
                return [(r["n"], r["rel"], r["m"]) for r in rows]
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
                                    continue
                                key_prop = "path" if "path" in props else "name"
                                non_key = {k: v for k, v in props.items() if k != key_prop}
                                # AGE 1.6 has no ON CREATE SET / ON MATCH SET;
                                # MERGE on the minimal key, then plain
                                # SET v += props for mutable properties
                                # (plan §3.3, same pattern as ingest.py).
                                cypher = (
                                    f"MERGE (v:{_check_label(label)} {{{key_prop}: {age_str(name)}}})\n"
                                    f"SET v += {age_props(non_key)}\n"
                                    f"RETURN id(v)"
                                )
                                row = await conn.fetchrow(
                                    f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (id agtype)"
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
                                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
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
                                    f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (id agtype)"
                                )
                                await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                if row is not None:
                                    done += 1
                            except Exception:
                                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
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
        cypher = (
            f"SELECT * FROM cypher('{graph}', $$ "
            "MATCH (c:Concept) "
            "OPTIONAL MATCH (c)-[r]-() "
            "WITH c, count(r) AS degree "
            "RETURN id(c), c.name, c.weight, c.created_at, degree "
            f"$$) AS (id agtype, name agtype, weight agtype, created_at agtype, degree agtype)"
        )
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
                    sp_fetch = savepoint_name("merge_fetch", 0)
                    await conn.execute(f"SAVEPOINT {sp_fetch}")
                    try:
                        edge_rows = await conn.fetch(
                            f"SELECT * FROM cypher('{graph}', $$ "
                            f"MATCH (loser:Concept) WHERE id(loser) = {loser_id} "
                            "MATCH (loser)-[r]->(m) RETURN type(r), id(m), r.weight, r.cosine "
                            f"$$) AS (t agtype, mid agtype, w agtype, c agtype)"
                        )
                        in_rows = await conn.fetch(
                            f"SELECT * FROM cypher('{graph}', $$ "
                            f"MATCH (loser:Concept) WHERE id(loser) = {loser_id} "
                            "MATCH (n)-[r]->(loser) RETURN type(r), id(n), r.weight, r.cosine "
                            f"$$) AS (t agtype, nid agtype, w agtype, c agtype)"
                        )
                        wrow = await conn.fetchrow(
                            f"SELECT * FROM cypher('{graph}', $$ "
                            f"MATCH (k:Concept) WHERE id(k) = {keeper_id} RETURN k.weight "
                            f"$$) AS (w agtype)"
                        )
                        lrow = await conn.fetchrow(
                            f"SELECT * FROM cypher('{graph}', $$ "
                            f"MATCH (l:Concept) WHERE id(l) = {loser_id} RETURN l.weight "
                            f"$$) AS (w agtype)"
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
                            props_str = f" SET e += {age_props(props)}" if props else ""
                            cypher = (
                                f"MATCH (a), (b) WHERE id(a) = {keeper_id} AND id(b) = {mid} "
                                f"MERGE (a)-[e:{label}]->(b){props_str} RETURN id(e)"
                            )
                            await conn.fetchrow(f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (id agtype)")
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
                            props_str = f" SET e += {age_props(props)}" if props else ""
                            cypher = (
                                f"MATCH (a), (b) WHERE id(a) = {nid} AND id(b) = {keeper_id} "
                                f"MERGE (a)-[e:{label}]->(b){props_str} RETURN id(e)"
                            )
                            await conn.fetchrow(f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (id agtype)")
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            logger.debug("merge incoming edge failed", exc_info=True)

                    sp_w = savepoint_name("merge_weight", 0)
                    await conn.execute(f"SAVEPOINT {sp_w}")
                    try:
                        cypher = f"MATCH (k:Concept) WHERE id(k) = {keeper_id} SET k += {age_props({'weight': new_weight})} RETURN id(k)"
                        await conn.fetchrow(f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (id agtype)")
                        await conn.execute(f"RELEASE SAVEPOINT {sp_w}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_w}")
                        logger.debug("merge weight update failed", exc_info=True)

                    sp_bridge = savepoint_name("merge_bridge", 0)
                    await conn.execute(f"SAVEPOINT {sp_bridge}")
                    try:
                        await conn.execute(
                            "UPDATE memory_chunk_nodes SET vertex_id = $1 WHERE vertex_id = $2",
                            keeper_id, loser_id,
                        )
                        await conn.execute(
                            "DELETE FROM memory_chunk_nodes a USING memory_chunk_nodes b "
                            "WHERE a.ctid < b.ctid AND a.chunk_id=b.chunk_id AND a.source=b.source AND a.vertex_id=b.vertex_id"
                        )
                        await conn.execute(f"RELEASE SAVEPOINT {sp_bridge}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_bridge}")
                        logger.debug("bridge merge failed", exc_info=True)

                    sp_del = savepoint_name("merge_delete", 0)
                    await conn.execute(f"SAVEPOINT {sp_del}")
                    try:
                        await conn.fetch(
                            f"SELECT * FROM cypher('{graph}', $$ MATCH (c:Concept) WHERE id(c) = {loser_id} DETACH DELETE c $$) AS (a agtype)"
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

        Ensures Concept vlabel exists; each delete in its own SAVEPOINT.
        Returns count of pruned vertices.
        """
        if not orphan_ids:
            return 0
        await self.ensure_about_labels()
        graph = self.graph_name
        pruned = 0
        async with self.pool.acquire() as conn:
            await self.load_age(conn)
            try:
                async with conn.transaction():
                    for idx, oid in enumerate(orphan_ids):
                        oid = int(oid)
                        sp = savepoint_name("prune_orphan", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            await conn.fetch(
                                f"SELECT * FROM cypher('{graph}', $$ MATCH (c:Concept) WHERE id(c) = {oid} DETACH DELETE c $$) AS (a agtype)"
                            )
                            await conn.execute("DELETE FROM memory_chunk_nodes WHERE vertex_id = $1", oid)
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
                            "SELECT count(*) FROM memory_chunk_nodes WHERE vertex_id = ANY($1::bigint[])",
                            all_loser_ids,
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
                        await conn.execute(f"SELECT drop_graph({age_str(name)})")
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
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
