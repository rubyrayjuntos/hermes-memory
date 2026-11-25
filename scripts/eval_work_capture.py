#!/usr/bin/env python3
"""G5 live-eval: re-run work-memory extraction (Spec 5 + Spec 7 session
splitting) against a real, already-replayed session corpus and report
session/segment/item counts plus the status (confirmed-rate) and
over-splitting metrics G5 actually gates on.

Run only against a disposable `*_eval` database that already has a real
conversation history replayed into it (never `hermes_memory_installed`).
This script WIPES that owner's prior work_item/work_event/
work_item_current/work_artifact/durable_job(extract_work) rows before
re-running, so each run reflects current code against the full corpus,
not a stale mix of old and new extraction passes. `conversations` rows
(and their embeddings) are never touched or wiped.

Status detection (`work_status.classify_verdict`/`infer_status`) is pure
rules, not a model call -- extract_mode is always rules_only here, so
re-running this costs nothing and needs no API key.

Usage:
    HYBRID_AGE_DSN=postgres://...@host/some_db_eval python3 scripts/eval_work_capture.py --owner linkibinki
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_memory import extraction_worker  # noqa: E402
from hermes_memory.config import HybridAgeConfig  # noqa: E402
from hermes_memory.store import Store  # noqa: E402
from hermes_memory.turn_boundaries import find_segment_boundaries  # noqa: E402


class _FakeProvider:
    def __init__(self, dsn: str):
        self._outbox_owner = "g5-eval-worker"
        self.config = HybridAgeConfig(
            dsn=dsn, work_capture=True, extract_mode="rules_only",
            work_verify_repo_roots=[],
        )


async def _embed(texts):
    # Status detection and segmentation are both rule-based (work_status.py,
    # turn_boundaries.py); the embedding stored on work_item rows plays no
    # part in either metric this script reports. A fixed dummy vector keeps
    # this run fast and free -- it is NOT a retrieval-quality check.
    return [[0.5] + [0.0] * 767 for _ in texts]


async def _assert_eval_db(pool) -> None:
    async with pool.acquire() as conn:
        dbname = await conn.fetchval("SELECT current_database()")
    assert "_eval" in dbname, f"refusing to start: connected db {dbname!r} is not *_eval*"
    assert dbname != "hermes_memory_installed", "refusing to start: connected to installed db"
    print(f"DB guard OK: connected database is {dbname!r}")


async def _wipe_prior_work_data(pool, owner: str) -> dict:
    async with pool.acquire() as conn:
        counts_before = {
            "work_item": await conn.fetchval("SELECT count(*) FROM work_item WHERE owner=$1", owner),
            "work_event": await conn.fetchval("SELECT count(*) FROM work_event WHERE owner=$1", owner),
            "work_artifact": await conn.fetchval(
                "SELECT count(*) FROM work_artifact WHERE owner=$1", owner),
            "durable_job_extract_work": await conn.fetchval(
                "SELECT count(*) FROM durable_job WHERE owner=$1 AND kind='extract_work'", owner),
        }
        async with conn.transaction():
            await conn.execute("DELETE FROM work_artifact WHERE owner=$1", owner)
            await conn.execute("DELETE FROM work_item_current WHERE owner=$1", owner)
            await conn.execute("DELETE FROM work_event WHERE owner=$1", owner)
            await conn.execute("DELETE FROM work_item WHERE owner=$1", owner)
            await conn.execute(
                "DELETE FROM durable_job WHERE owner=$1 AND kind='extract_work'", owner)
    print(f"Wiped prior work-memory rows for {owner!r}: {counts_before}")
    return counts_before


async def _enqueue_all_sessions(pool, owner: str) -> dict:
    async with pool.acquire() as conn:
        session_ids = [r["session_id"] for r in await conn.fetch(
            "SELECT DISTINCT session_id FROM conversations WHERE agent_identity=$1", owner
        )]
    print(f"{len(session_ids)} distinct sessions to segment+enqueue")
    store = Store(pool, graph_name="hermes_knowledge")
    total_segments = 0
    per_session_segment_counts: dict[str, int] = {}
    for i, session_id in enumerate(session_ids):
        async with pool.acquire() as conn:
            spans = await conn.fetch(
                "SELECT id, role, content FROM conversations "
                "WHERE agent_identity=$1 AND session_id=$2 ORDER BY id",
                owner, session_id,
            )
        if not spans:
            continue
        segments = find_segment_boundaries(
            [{"id": s["id"], "role": s["role"], "content": s["content"]} for s in spans]
        )
        per_session_segment_counts[session_id] = len(segments)
        total_segments += len(segments)
        for seg in segments:
            op_id = f"extract_work:{session_id}:{seg.first_span_id}-{seg.last_span_id}"
            payload = {
                "owner": owner, "session_id": session_id,
                "first_span_id": seg.first_span_id, "last_span_id": seg.last_span_id,
            }
            await store.job_add(owner, "extract_work", op_id, payload, session_id=session_id)
        if (i + 1) % 100 == 0:
            print(f"  enqueued {i + 1}/{len(session_ids)} sessions, {total_segments} segments so far")
    print(f"Enqueued {total_segments} extract_work jobs across {len(session_ids)} sessions")
    return {"n_sessions": len(session_ids), "n_segments": total_segments,
            "per_session_segment_counts": per_session_segment_counts}


async def _drain(pool, dsn: str) -> int:
    provider = _FakeProvider(dsn)
    store = Store(pool, graph_name="hermes_knowledge")
    finished_total = 0
    idle_rounds = 0
    while idle_rounds < 3:
        finished = await extraction_worker.run_extraction_once(
            provider, store, embed=_embed, model=None)
        finished_total += finished
        idle_rounds = 0 if finished else idle_rounds + 1
        if finished_total and finished_total % 200 == 0:
            print(f"  drained {finished_total} extract_work jobs so far")
    print(f"Drain complete: {finished_total} extract_work jobs settled")
    return finished_total


async def _compute_metrics(pool, owner: str, enqueue_stats: dict, over_split_threshold: int) -> dict:
    async with pool.acquire() as conn:
        work_item_rows = await conn.fetch(
            "SELECT session_id, status FROM work_item WHERE owner=$1", owner)
        durable_job_status = await conn.fetch(
            "SELECT status, count(*) AS n FROM durable_job "
            "WHERE owner=$1 AND kind='extract_work' GROUP BY status", owner)

    n_items = len(work_item_rows)
    items_per_session: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for r in work_item_rows:
        items_per_session[r["session_id"]] = items_per_session.get(r["session_id"], 0) + 1
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    confirmed = sum(status_counts.get(s, 0) for s in ("done", "failed", "abandoned"))
    max_items_session = max(items_per_session.values()) if items_per_session else 0
    over_split_sessions = {
        sid: n for sid, n in items_per_session.items() if n > over_split_threshold
    }
    over_split_item_share = (
        sum(over_split_sessions.values()) / n_items if n_items else 0.0
    )

    return {
        "n_sessions_enqueued": enqueue_stats["n_sessions"],
        "n_segments_enqueued": enqueue_stats["n_segments"],
        "n_work_items_produced": n_items,
        "n_sessions_with_items": len(items_per_session),
        "max_items_in_one_session": max_items_session,
        "over_split_threshold": over_split_threshold,
        "over_split_sessions": over_split_sessions,
        "over_split_item_share": round(over_split_item_share, 4),
        "status_distribution": status_counts,
        "metric_b_status": {
            "confirmed": confirmed,
            "confirmed_rate": round(confirmed / n_items, 4) if n_items else 0.0,
            "unknown": status_counts.get("unknown", 0),
            "unknown_rate": round(status_counts.get("unknown", 0) / n_items, 4) if n_items else 0.0,
        },
        "durable_job_extract_work_final_status": {
            r["status"]: r["n"] for r in durable_job_status
        },
    }


async def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn", default=os.environ.get("HYBRID_AGE_DSN"),
                    help="defaults to $HYBRID_AGE_DSN")
    p.add_argument("--owner", required=True, help="agent_identity to re-extract for")
    p.add_argument("--over-split-threshold", type=int, default=20,
                    help="a session producing more items than this is flagged as over-split")
    p.add_argument("--out", default="g5_eval_report.json")
    args = p.parse_args(argv)

    if not args.dsn:
        print("no DSN: pass --dsn or set HYBRID_AGE_DSN", file=sys.stderr)
        return 2
    assert "_eval" in args.dsn, "refusing to start: DSN does not name an *_eval* database"
    assert "hermes_memory_installed" not in args.dsn, "refusing to start: points at installed db"

    import asyncpg
    pool = await asyncpg.create_pool(args.dsn, min_size=2, max_size=8)
    try:
        await _assert_eval_db(pool)
        wiped = await _wipe_prior_work_data(pool, args.owner)
        enqueue_stats = await _enqueue_all_sessions(pool, args.owner)
        finished = await _drain(pool, args.dsn)
        metrics = await _compute_metrics(pool, args.owner, enqueue_stats, args.over_split_threshold)
        report = {
            "run": "G5 live eval (Spec 5 work-memory + Spec 7 session splitting)",
            "owner": args.owner,
            "wiped_prior_rows": wiped,
            "jobs_finished_in_drain": finished,
            "note_on_embeddings": (
                "work_item.embedding is a fixed dummy vector in this run -- this "
                "pass measures extraction/segmentation/status quality only, not "
                "work-item retrieval quality."
            ),
            **metrics,
        }
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nReport written: {args.out}")
        print(json.dumps(metrics, indent=2, default=str))
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
