"""Batched AGE MERGE (vertices/edges) — SAVEPOINT per statement."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import asyncpg

from .age_cypher import (
    _check_label,
    _savepoint_name,
    age_props,
    age_str,
    cypher_call,
)

logger = logging.getLogger("hybrid_age.store")


class StoreMergeMixin:
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
                                except asyncpg.PostgresError:
                                    pass
                                try:
                                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                except asyncpg.PostgresError:
                                    pass
                                logger.warning("vertex MERGE failed %s", props, exc_info=True)
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
                                except asyncpg.PostgresError:
                                    pass
                                try:
                                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                                except asyncpg.PostgresError:
                                    pass
                                logger.warning("edge MERGE failed", exc_info=True)
                except Exception:
                    logger.warning("edge batch txn failed", exc_info=True)
        return done


