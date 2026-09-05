"""AGE Concept compaction / fetch / purge (legacy ABOUT catalog)."""
from __future__ import annotations

import logging
from typing import Sequence

from .age_cypher import (
    age_props,
    age_str,
    check_label,
    cypher_call,
    savepoint_name,
)

logger = logging.getLogger("hybrid_age.store")


class StoreConceptsMixin:
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
                        logger.warning("fetch_concepts failed", exc_info=True)
                        return []
            except Exception:
                logger.warning("fetch_concepts txn failed", exc_info=True)
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
                        logger.debug("fetch_concept_names cypher failed", exc_info=True)
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        return []
            except Exception:
                logger.debug("fetch_concept_names txn failed", exc_info=True)
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
                        logger.warning("merge_concept_pair fetch failed", exc_info=True)
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
                            logger.warning("merge outgoing edge failed", exc_info=True)
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
                            logger.warning("merge incoming edge failed", exc_info=True)
                            raise

                    sp_w = savepoint_name("merge_weight", 0)
                    await conn.execute(f"SAVEPOINT {sp_w}")
                    try:
                        cypher = f"MATCH (k:Concept) WHERE id(k) = {keeper_id} SET k += {age_props({'weight': new_weight})} RETURN id(k)"
                        await conn.fetchrow(f"SELECT * FROM {cypher_call(graph, cypher)} AS (id agtype)")
                        await conn.execute(f"RELEASE SAVEPOINT {sp_w}")
                    except Exception:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_w}")
                        logger.warning("merge weight update failed", exc_info=True)
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
                        logger.warning("bridge merge failed", exc_info=True)
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
                        logger.warning("loser DETACH DELETE failed", exc_info=True)
                        return False
            except Exception:
                logger.warning("merge_concept_pair transaction failed", exc_info=True)
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

