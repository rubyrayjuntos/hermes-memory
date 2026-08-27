"""hermes_memory.loadgen — synthetic corpus generator with bridge population.

Card C8: adapts spec Appendix 12.1 (DESIGN SKETCH ONLY) to the real v0.2
schema:
- Rows land in ``memory_entries`` (content/target columns, BIGINT ids,
  vector(768)) tagged ``agent_identity='loadgen'`` so they are identifiable
  and filterable.
- Bridges land in ``memory_chunk_nodes`` twice per entity:
  ``chunk_id='lg_<uuid5>'`` (card-mandated marker key) AND
  ``chunk_id='<row-id>'`` (what ``bench.cli.BenchHarness._graph_expand``
  actually looks up — without this the C7 ablation never sees expansion).
- Graph: ``(:Entity {name})`` vertices named with an ``lg_`` prefix,
  MERGEd on the minimal key ``{name}`` then ``SET v += {...}`` (AGE 1.6 has
  no ON CREATE/MATCH SET), edges labelled with the same relation vocabulary
  the bench expands on, every Cypher statement inside a SAVEPOINT.
- Deterministic: seeded RNG for structure, per-row seeded RNG for vectors;
  re-running the same --seed/--size tops up rather than duplicates
  (deterministic chunk keys stored in metadata).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import time
import uuid
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import asyncpg

from .store import age_props, age_str, check_label, savepoint_name, validate_graph_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hybrid_age.loadgen")

AGENT_IDENTITY = "loadgen"


def _stable_hash(name: str) -> int:
    """Deterministic hash for savepoint names (PYTHONHASHSEED-proof)."""
    return int(hashlib.sha1(name.encode()).hexdigest()[:8], 16)


SOURCE = "loadgen"
GRAPH_NAME = "hermes_knowledge"
DEFAULT_DSN = (
    f"postgres://hermes:{os.environ.get('HERMES_PG_PASSWORD', 'hermes')}"
    "@localhost:5450/hermes_memory"
)
BATCH_SIZE = 100          # >=50 statements per transaction (apache/age#2177)
NAMESPACE = uuid.UUID("c8a7f9e0-1111-4c08-b007-000000000001")  # stable namespace

BASE_ENTITIES = [
    "AuthModule", "RateLimiter", "RedisCache", "PostgresDB", "UserSession",
    "HermesCore", "GraphExtractor", "VectorStore", "Embedder", "APIWorker",
]
# Relation vocabulary MUST stay aligned with bench.cli._graph_expand's
# expanded rel types or the ablation never fires.
ACTIONS = ["RELATED_TO", "WORKS_ON", "USES", "DEPENDS_ON", "BUILT_WITH"]

TEMPLATES = [
    "In the latest architecture review, we decided that {e1} {action} {e2}.",
    "User mentioned having issues when {e1} {action} {e2}.",
    "Note: ensure {e1} {action} {e2} before deployment.",
    "Follow-up: the ticket tracks how {e1} {action} {e2} this quarter.",
    "Design doc excerpt: {e1} {action} {e2}; revisit after the migration.",
    "Standup note — {e1} {action} {e2}, owner assigned, ETA next sprint.",
]


def _entity_pool(size: int) -> List[str]:
    """10 base entities x numbered suffixes; pool sized so each entity shows
    up in dozens of chunks (~5 shared neighbours per chunk at every size)."""
    suffixes = max(1, size // 500)
    return [f"lg_{base}_{k}" for base in BASE_ENTITIES for k in range(1, suffixes + 1)]


def _vector_literal(rng: random.Random, dim: int = 768) -> str:
    """Random-but-normalized vector literal, deterministic from rng."""
    vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    mag = sum(x * x for x in vec) ** 0.5 or 1.0
    return "[" + ",".join(f"{x / mag:.6f}" for x in vec) + "]"


def iter_corpus(size: int, seed: int) -> Iterator[dict]:
    """Lazily yield deterministic chunk specs (streaming variant of
    ``build_corpus``): same RNG draw order per row, so output is byte-for-byte
    identical to materializing the whole corpus — without holding 100K dicts
    (~1GB) in RAM."""
    rng = random.Random(seed)
    pool = _entity_pool(size)
    seen: set = set()
    for i in range(size):
        e1, e2 = rng.sample(pool, 2)
        action = rng.choice(ACTIONS)
        # guarantee content uniqueness against the (agent,target,content)
        # UNIQUE key: vary template until unseen
        templates = rng.sample(TEMPLATES, len(TEMPLATES))
        content = None
        for t in templates:
            cand = t.format(e1=e1, action=action.lower().replace("_", " "), e2=e2)
            if cand not in seen:
                content = cand
                break
        if content is None:  # pathological; disambiguate with the key
            content = (f"[{seed}:{i}] " + templates[0].format(
                e1=e1, action=action.lower().replace("_", " "), e2=e2))
        seen.add(content)
        vrng = random.Random(f"{seed}:{i}")
        yield {
            "key": f"{seed}:{i}",
            "chunk_id": "lg_" + str(uuid.uuid5(NAMESPACE, f"{seed}:{i}")),
            "content": content,
            "target": "synthetic",
            "entities": (e1, e2),
            "action": action,
            "vec": _vector_literal(vrng),
        }


def build_corpus(size: int, seed: int) -> List[dict]:
    """Materialized corpus (kept for callers/tests); ``generate`` streams via
    :func:`iter_corpus` instead."""
    return list(iter_corpus(size, seed))


class LoadGen:
    def __init__(self, dsn: str, graph_name: str = GRAPH_NAME):
        self.dsn = dsn
        self.graph_name = validate_graph_name(graph_name)
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # -- graph primitives (SAVEPOINT-wrapped, batched >=50) ------------------

    async def _merge_entity(self, conn, name: str) -> Optional[int]:
        """MERGE (:Entity {name}) minimal-key, SET += props. Returns bigint id."""
        sp = savepoint_name("lg_v", _stable_hash(name))
        await conn.execute(f"SAVEPOINT {sp}")
        try:
            cypher = (
                f"MERGE (v:{check_label('Entity')} {{name: {age_str(name)}}})\n"
                f"SET v += {age_props({'source': SOURCE, 'kind': 'synthetic'})}\n"
                f"RETURN id(v)"
            )
            row = await conn.fetchrow(
                f"SELECT * FROM cypher('{self.graph_name}', $$ {cypher} $$) AS (id agtype)"
            )
            await conn.execute(f"RELEASE SAVEPOINT {sp}")
            return int(str(row["id"]).strip('"'))
        except Exception:
            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            logger.warning("entity MERGE failed for %s", name, exc_info=True)
            return None

    async def _merge_edge(self, conn, src: int, dst: int, label: str,
                          source_chunk: str) -> bool:
        sp = savepoint_name("lg_e", _stable_hash(source_chunk))
        await conn.execute(f"SAVEPOINT {sp}")
        try:
            cypher = (
                f"MATCH (a), (b) WHERE id(a) = {int(src)} AND id(b) = {int(dst)} "
                f"MERGE (a)-[e:{check_label(label)}]->(b) "
                f"SET e += {age_props({'source_chunk': source_chunk, 'source': SOURCE})} "
                f"RETURN id(e)"
            )
            row = await conn.fetchrow(
                f"SELECT * FROM cypher('{self.graph_name}', $$ {cypher} $$) AS (id agtype)"
            )
            await conn.execute(f"RELEASE SAVEPOINT {sp}")
            return row is not None
        except Exception:
            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            logger.debug("edge MERGE failed %s->%s", src, dst, exc_info=True)
            return False

    # -- main generation ------------------------------------------------------

    async def generate(self, size: int, seed: int) -> dict:
        assert self.pool is not None
        t0 = time.perf_counter()

        async with self.pool.acquire() as conn:
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, public;")
            existing = {
                r[0] for r in await conn.fetch(
                    "SELECT metadata->>'lg_key' FROM memory_entries "
                    "WHERE agent_identity = $1 AND metadata->>'lg_key' IS NOT NULL",
                    AGENT_IDENTITY,
                )
            }
        skipped = sum(
            1 for c in iter_corpus(size, seed) if c["key"] in existing
        )
        todo = size - skipped
        logger.info("%d chunks already present (top-up mode); generating %d",
                    skipped, todo)

        vid_cache: Dict[str, Optional[int]] = {}
        inserted = bridged = edges_done = failed_batches = 0

        def batches() -> Iterator[List[dict]]:
            buf: List[dict] = []
            for c in iter_corpus(size, seed):
                if c["key"] in existing:
                    continue
                buf.append(c)
                if len(buf) == BATCH_SIZE:
                    yield buf
                    buf = []
            if buf:
                yield buf

        for bno, batch in enumerate(batches()):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("LOAD 'age';")
                    await conn.execute("SET search_path = ag_catalog, public;")
                    async with conn.transaction():
                        # 1. relational rows, one INSERT..RETURNING per batch
                        rows = await conn.fetch(
                            """
                            INSERT INTO memory_entries
                                (agent_identity, target, content, embedding, metadata)
                            SELECT * FROM unnest($1::text[], $2::text[], $3::text[],
                                                 $4::vector[], $5::jsonb[])
                                 AS t(agent_identity, target, content, embedding, metadata)
                            RETURNING id
                            """,
                            [AGENT_IDENTITY] * len(batch),
                            [c["target"] for c in batch],
                            [c["content"] for c in batch],
                            [c["vec"] for c in batch],
                            [json.dumps({"lg_key": c["key"], "lg_chunk_id": c["chunk_id"],
                                         "source": SOURCE}) for c in batch],
                        )
                        inserted += len(rows)
                        # 2. vertices (SAVEPOINT per MERGE) + bridge rows
                        bridge_rows = []  # (chunk_id, vertex_id)
                        for c, r in zip(batch, rows):
                            row_key = str(r["id"])
                            for ent in c["entities"]:
                                if ent not in vid_cache:
                                    vid_cache[ent] = await self._merge_entity(conn, ent)
                                vid = vid_cache[ent]
                                if vid is None:
                                    continue
                                bridge_rows.append((c["chunk_id"], vid))
                                bridge_rows.append((row_key, vid))
                            v1 = vid_cache.get(c["entities"][0])
                            v2 = vid_cache.get(c["entities"][1])
                            if v1 is not None and v2 is not None:
                                ok = await self._merge_edge(
                                    conn, v1, v2, c["action"], c["chunk_id"])
                                edges_done += int(ok)
                        # 3. bridge rows (PK dedups on re-run)
                        await conn.executemany(
                            """
                            INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id)
                            VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                            """,
                            [(cid, SOURCE, int(vid)) for cid, vid in bridge_rows],
                        )
                        bridged += len(bridge_rows)
                logger.info("batch %d done (%d entries inserted so far)",
                            bno, inserted)
            except Exception:
                # Batch-level isolation: one bad batch must not kill the run.
                # The top-up key check makes re-running cheap — the failed
                # keys are simply absent and get retried on the next pass.
                failed_batches += 1
                logger.error("batch %d failed; continuing (%d/%d batches failed)",
                             bno, failed_batches, bno + 1, exc_info=True)

        stats = await self.coverage_stats()
        stats.update({
            "generated": inserted, "skipped_existing": skipped,
            "bridge_rows_written": bridged, "edges_merged": edges_done,
            "failed_batches": failed_batches,
            "wall_seconds": round(time.perf_counter() - t0, 1),
        })
        return stats

    async def coverage_stats(self) -> dict:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            n_entries = await conn.fetchval(
                "SELECT count(*) FROM memory_entries WHERE agent_identity=$1",
                AGENT_IDENTITY)
            n_bridges = await conn.fetchval(
                "SELECT count(DISTINCT chunk_id) FROM memory_chunk_nodes WHERE source=$1",
                SOURCE)
            n_bridge_rows = await conn.fetchval(
                "SELECT count(*) FROM memory_chunk_nodes WHERE source=$1", SOURCE)
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, public;")
            try:
                row = await conn.fetchrow(
                    f"""SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (v:Entity) WHERE v.name STARTS WITH 'lg_'
                        OPTIONAL MATCH (v)-[r]-()
                        WITH count(DISTINCT v) AS ents,
                             count(DISTINCT r) AS deg
                        RETURN ents, deg
                    $$) AS (ents agtype, deg agtype)""")
                ents = int(str(row["ents"])) if row else 0
                deg = float(str(row["deg"])) if row else 0.0
            except Exception:
                logger.warning("graph stats query failed", exc_info=True)
                ents, deg = 0, 0.0
        return {
            "entries": int(n_entries or 0),
            "chunks_with_bridge": int(n_bridges or 0),
            "bridge_rows": int(n_bridge_rows or 0),
            "lg_entities": ents,
            # count(DISTINCT r): each undirected edge touches v from both
            # sides, so a plain count(r) would double every edge.
            "avg_degree_per_entity": round(deg / ents, 2) if ents else 0.0,
        }

    async def teardown(self) -> dict:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, public;")
            n_bridges = await conn.fetchval(
                "WITH d AS (DELETE FROM memory_chunk_nodes WHERE source=$1 "
                "OR chunk_id LIKE 'lg\\_%' ESCAPE '\\' RETURNING 1) "
                "SELECT count(*) FROM d", SOURCE)
            n_entries = await conn.fetchval(
                "WITH d AS (DELETE FROM memory_entries WHERE agent_identity=$1 "
                "RETURNING 1) SELECT count(*) FROM d", AGENT_IDENTITY)
            deleted_vertices = 0
            async with conn.transaction():
                sp = savepoint_name("lg_teardown", 0)
                await conn.execute(f"SAVEPOINT {sp}")
                try:
                    rows = await conn.fetch(
                        f"""SELECT * FROM cypher('{self.graph_name}', $$
                            MATCH (v:Entity) WHERE v.name STARTS WITH 'lg_'
                            DETACH DELETE v RETURN count(*) AS n
                        $$) AS (n agtype)""")
                    deleted_vertices = int(str(rows[0]["n"])) if rows else 0
                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    logger.warning("teardown DETACH DELETE failed", exc_info=True)
        return {"entries_deleted": int(n_entries or 0),
                "bridge_rows_deleted": int(n_bridges or 0),
                "vertices_deleted": deleted_vertices}


async def cmd(args: argparse.Namespace) -> int:
    cfg_dsn = os.environ.get("HYBRID_AGE_DSN")
    dsn = args.dsn or cfg_dsn or DEFAULT_DSN
    gen = LoadGen(dsn)
    await gen.connect()
    try:
        if args.teardown:
            out = await gen.teardown()
            print(json.dumps(out, indent=2))
        else:
            out = await gen.generate(args.size, args.seed)
            print("\n## Load generation complete\n")
            print(f"| Metric                  | Value |")
            print(f"|-------------------------|-------|")
            for k, v in out.items():
                print(f"| {k:<23} | {v:>5} |")
    finally:
        await gen.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hermes_memory.loadgen",
                                description="synthetic load generator (C8)")
    p.add_argument("--size", type=int, default=1000, choices=[1000, 10000, 100000])
    p.add_argument("--dsn", default=None, help="override DSN")
    p.add_argument("--seed", type=int, default=42, help="deterministic RNG seed")
    p.add_argument("--teardown", action="store_true",
                   help="delete all loadgen rows, bridges and :Entity vertices")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(cmd(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
