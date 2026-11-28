"""Full-corpus ingest for the LongMemEval 500-question, 3-condition run,
pinned to v1.0.1 (includes #123's fix). This is the one-time ingest into
ONE throwaway *_eval database, shared by the tiered-off and tiered-on
conditions (they differ only in retrieval-time config, not in what's
stored). The no-memory condition needs no ingest at all.

Resumable: a checkpoint file records each completed question_id with its
timing; a restart reads it and skips anything already done, so a crash
doesn't restart from zero. Never --reuse-dsn semantics apply to the
DATABASE (fresh, built from migrations) -- the checkpoint is this script's
own resumability, not a reused eval run.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "/home/rswan/Documents/Codex/hermes-memory/src")

from hermes_memory.config import load_config  # noqa: E402
from hermes_memory.provider import HybridAgeMemoryProvider  # noqa: E402

DATASET_PATH = "/home/rswan/Documents/Codex/LongMemEval/data/longmemeval_s_cleaned.json"
CHECKPOINT_PATH = "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_full_ingest_checkpoint.json"


def queue_full_guard(provider: HybridAgeMemoryProvider, high_water: float = 0.6) -> None:
    q = provider._write_queue
    if q is None:
        return
    limit = int(q.maxsize * high_water)
    while q.qsize() > limit:
        time.sleep(0.02)


def wait_for_drain(provider: HybridAgeMemoryProvider, stable_for_s: float = 2.0,
                    max_wait_s: float = 300.0) -> int:
    async def _poll():
        async with provider.pool.acquire() as conn:
            last = -1
            stable_since = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < max_wait_s:
                count = await conn.fetchval("SELECT count(*) FROM conversations")
                queue_empty = provider._write_queue is None or provider._write_queue.empty()
                if count == last and queue_empty:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= stable_for_s:
                        return count
                else:
                    stable_since = None
                    last = count
                await asyncio.sleep(0.1)
            return last

    return provider._run(_poll(), timeout=max_wait_s + 5)


def load_checkpoint() -> dict:
    try:
        return json.load(open(CHECKPOINT_PATH))
    except FileNotFoundError:
        return {"done": {}, "owner_map": {}}


def save_checkpoint(ckpt: dict) -> None:
    json.dump(ckpt, open(CHECKPOINT_PATH, "w"), indent=2)


def main():
    dataset = json.load(open(DATASET_PATH))
    ckpt = load_checkpoint()
    done = ckpt["done"]
    owner_map = ckpt["owner_map"]

    provider = HybridAgeMemoryProvider(config=load_config())
    provider.config.queue_maxsize = 4096
    # NOTE: initialize() is idempotent for loop/pool/thread re-creation, but
    # it DOES re-resolve _agent_identity on every call (provider.py:175) --
    # that's the hook used below to re-point the SAME provider instance at a
    # fresh owner per question, so each question's haystack is genuinely
    # isolated (same shared embedder, no per-question pool/loop churn).

    t_run_start = time.monotonic()
    remaining = [q for q in dataset if q["question_id"] not in done]
    print(f"{len(done)}/{len(dataset)} already done, {len(remaining)} remaining", flush=True)

    for i, q in enumerate(dataset, start=1):
        qid = q["question_id"]
        if qid in done:
            continue
        owner = owner_map.get(qid) or f"lme500_q{i:03d}_{qid}"
        owner_map[qid] = owner
        provider.initialize(session_id=f"{owner}:ingest", agent_identity=owner)
        sessions = q["haystack_sessions"]
        n_turns = sum(len(s) for s in sessions)

        t0 = time.monotonic()
        for session, session_id in zip(sessions, q["haystack_session_ids"]):
            for turn in session:
                role = turn.get("role", "user")
                content = turn.get("content", "") or ""
                if not content:
                    continue
                queue_full_guard(provider)
                if role == "user":
                    provider.sync_turn(
                        user_content=content, assistant_content="",
                        session_id=f"{owner}:{session_id}",
                    )
                else:
                    provider.sync_turn(
                        user_content="", assistant_content=content,
                        session_id=f"{owner}:{session_id}",
                    )
        landed_total = wait_for_drain(provider)
        elapsed = time.monotonic() - t0
        done[qid] = {
            "order": i, "owner": owner, "n_turns": n_turns,
            "seconds": elapsed, "cumulative_landed_at_finish": landed_total,
        }
        ckpt["done"] = done
        ckpt["owner_map"] = owner_map
        save_checkpoint(ckpt)
        n_done = len(done)
        print(
            f"[{n_done:3d}/500] {qid}: {n_turns:4d} turns, drain-complete in "
            f"{elapsed:7.2f}s ({elapsed/max(n_turns,1)*1000:.1f} ms/turn) -- "
            f"cumulative DB rows {landed_total} -- session elapsed "
            f"{time.monotonic()-t_run_start:.1f}s",
            flush=True,
        )

    provider.shutdown()
    print("INGEST COMPLETE: all 500 questions landed.")


if __name__ == "__main__":
    main()
