"""Growth check for issue #123's fix: ingest the frozen 20-question
LongMemEval dry-run sample into one fresh, continuously-growing eval DB,
and record seconds/question vs question order. If the line is flat, the
fix held and the full 500-question run's timing projection is trustworthy.
If it still climbs, there's another unscoped query to find.

sync_turn() is non-blocking (enqueue only); the real per-turn work (the
noun-table lookup included) happens asynchronously in a background drain
thread. Timing only the enqueue loop would measure queue-push speed, not
real work, and would look flat regardless of #123 -- so each question's
"seconds" is measured until the real `conversations` row count stops
changing (wait_for_drain), not until the enqueue loop returns.

One long-lived provider for the whole run (the #122 shared-embedder fix
means this doesn't reload the ONNX model per question), and the SAME db
growing across all 20 questions (unlike the original dry run's per-question
isolation) -- that's the point: growth as the table grows, not isolated
per-question cost.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "/home/rswan/Documents/Codex/hermes-memory/src")

from hermes_memory.config import load_config  # noqa: E402
from hermes_memory.provider import HybridAgeMemoryProvider  # noqa: E402

FROZEN_PATH = "/home/rswan/Documents/Codex/hermes-memory/docs/evals/longmemeval/dryrun_20_questions_frozen.json"
DATASET_PATH = "/home/rswan/Documents/Codex/LongMemEval/data/longmemeval_s_cleaned.json"
OUTPUT_PATH = "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/growth_check_results.json"


def queue_full_guard(provider: HybridAgeMemoryProvider, high_water: float = 0.6) -> None:
    """Issue #119: sync_turn()'s enqueue is non-blocking and silently drops
    on QueueFull under bulk-ingest speeds (confirmed in a first attempt at
    this very script -- 'enqueue failed ... QueueFull' in the log). Pace
    ingest instead of hitting the harness-level QueueFull bug: block briefly
    whenever the queue is more than high_water full, same mitigation the
    original dry-run harness used."""
    q = provider._write_queue
    if q is None:
        return
    limit = int(q.maxsize * high_water)
    while q.qsize() > limit:
        time.sleep(0.02)


def wait_for_drain(provider: HybridAgeMemoryProvider, stable_for_s: float = 2.0,
                    max_wait_s: float = 300.0) -> int:
    """The real signal for 'this question's writes are actually done,
    including whatever noun-table work ran'. Requires BOTH: the in-process
    write queue is empty, AND the real landed row count has stopped
    changing for `stable_for_s` seconds. Row-count-only stability was not
    enough on its own -- confirmed by a real run where the reported
    "landed" total for question 1 (53) was stale: the true cumulative count
    had already climbed past it (131) two questions later, meaning rows
    kept landing well after this function declared the question done. The
    queue can look briefly idle between bursts (a worker between items,
    embedding batches) without actually being finished."""
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


def main():
    frozen = json.load(open(FROZEN_PATH))
    dataset = json.load(open(DATASET_PATH))
    by_id = {q["question_id"]: q for q in dataset}
    ordered_qids = [q["question_id"] for q in frozen["questions"]]
    missing = [qid for qid in ordered_qids if qid not in by_id]
    if missing:
        print(f"ERROR: {len(missing)} frozen question_ids not found in dataset: {missing}")
        sys.exit(1)

    provider = HybridAgeMemoryProvider(config=load_config())
    provider.config.queue_maxsize = 4096
    provider.initialize(session_id="growth-check-session")

    results = []
    cumulative_landed = 0
    t_run_start = time.monotonic()
    for i, qid in enumerate(ordered_qids, start=1):
        q = by_id[qid]
        owner = f"growthcheck_q{i:02d}_{qid}"
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
        elapsed_enqueue = time.monotonic() - t0
        landed = wait_for_drain(provider)
        elapsed = time.monotonic() - t0
        landed_this_question = landed - cumulative_landed
        cumulative_landed = landed
        results.append({
            "order": i, "question_id": qid, "n_turns": n_turns,
            "seconds": elapsed, "seconds_per_turn": elapsed / max(n_turns, 1),
            "seconds_enqueue_only": elapsed_enqueue,
            "landed_this_question": landed_this_question,
            "cumulative_landed": landed,
        })
        print(
            f"[{i:2d}/20] {qid}: {n_turns:4d} turns, {landed_this_question:4d} landed, "
            f"drain-complete in {elapsed:7.2f}s ({elapsed/max(n_turns,1)*1000:.1f} ms/turn) "
            f"-- cumulative DB rows {landed} -- total elapsed "
            f"{time.monotonic()-t_run_start:.1f}s",
            flush=True,
        )
        json.dump(results, open(OUTPUT_PATH, "w"), indent=2)

    provider.shutdown()
    print(f"DONE. Results written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
