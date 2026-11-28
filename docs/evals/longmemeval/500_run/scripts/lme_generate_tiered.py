"""Generation for tiered_off / tiered_on, using hermes-memory's real
provider.prefetch() against the already-ingested eval database. Records:
  - lookup latency (wall time of the prefetch() call itself)
  - injected token estimate (len(injected_text)/4, consistent proxy with
    the rest of this harness -- not billed, so an exact tokenizer isn't
    required)
  - retrieval recall: which evidence sessions/turns (LongMemEval's
    answer_session_ids + has_answer-flagged turns) appear among the turn
    ids actually present in the injected text block, at both session and
    turn level.
  - any #118/#119-shaped log events observed during this question's
    prefetch (best-effort grep of recent provider logger output is not
    wired here; #118/#119 are ingest-time events already captured in
    lme_full_ingest.log, so this script reports ingest-time events only
    by reference, not by re-detecting them at read time).

Only runs on questions already present in the ingest checkpoint's "done"
map -- anything not yet landed is skipped and reported as pending so a
partial-ingest run doesn't silently generate against an empty/partial
owner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/rswan/Documents/Codex/hermes-memory/src")
sys.path.insert(0, "/home/rswan/.hermes/profiles/linkibinki/cache/scratch")

from hermes_memory.config import load_config  # noqa: E402
from hermes_memory.provider import HybridAgeMemoryProvider  # noqa: E402

from lme_openrouter_client import ANSWER_MODEL, Ledger, SpendCapHit, call_openrouter  # noqa: E402

DATASET_PATH = "/home/rswan/Documents/Codex/LongMemEval/data/longmemeval_s_cleaned.json"
INGEST_CHECKPOINT = "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_full_ingest_checkpoint.json"
TRACE_DIR = Path("/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_traces")
TRACE_DIR.mkdir(exist_ok=True)

GEN_LENGTH = 500
HISTORY_TEMPLATE = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history.\n\n\n"
    "History Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer:"
)
SPAN_ID_RE = re.compile(r'<span[^>]*\bid="(\d+)"')


def load_trace(condition: str) -> dict:
    path = TRACE_DIR / f"{condition}.jsonl"
    done = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["question_id"]] = row
    return done


def append_trace(condition: str, row: dict) -> None:
    with open(TRACE_DIR / f"{condition}.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def evidence_ground_truth(q: dict) -> tuple[set[str], list[tuple[str, str]]]:
    """Returns (evidence_session_ids, [(session_id, content_prefix), ...])
    for has_answer-flagged turns -- the turn-level recall unit."""
    evidence_sessions = set(q.get("answer_session_ids") or [])
    evidence_turns = []
    for sess_id, sess in zip(q["haystack_session_ids"], q["haystack_sessions"]):
        for turn in sess:
            if turn.get("has_answer"):
                evidence_turns.append((sess_id, (turn.get("content") or "")[:120]))
    return evidence_sessions, evidence_turns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", required=True, choices=["tiered_off", "tiered_on"])
    args = ap.parse_args()
    tiered_on = args.condition == "tiered_on"

    dataset = json.load(open(DATASET_PATH))
    dataset_by_id = {q["question_id"]: q for q in dataset}
    ckpt = json.load(open(INGEST_CHECKPOINT))
    owner_map = ckpt["owner_map"]
    ingested_qids = set(ckpt["done"].keys())

    done = load_trace(args.condition)
    remaining = [qid for qid in dataset_by_id if qid in ingested_qids and qid not in done]
    not_yet_ingested = [qid for qid in dataset_by_id if qid not in ingested_qids]
    print(f"{args.condition}: {len(done)} done, {len(remaining)} remaining "
          f"(ingested so far), {len(not_yet_ingested)} not yet ingested", flush=True)

    ledger = Ledger("main_three", cap_usd=15.0)
    provider = HybridAgeMemoryProvider(config=load_config())
    provider.config.queue_maxsize = 4096
    provider.config.tiered_injection = tiered_on

    for qid in remaining:
        q = dataset_by_id[qid]
        owner = owner_map[qid]
        provider.initialize(session_id=f"{owner}:gen", agent_identity=owner)
        provider.config.tiered_injection = tiered_on  # re-assert every call

        t0 = time.monotonic()
        injected = provider.prefetch(q["question"], session_id=f"{owner}:gen:q")
        lookup_latency_s = time.monotonic() - t0

        turn_ids = [int(m) for m in SPAN_ID_RE.findall(injected)]
        session_ids_hit, turn_contents_hit = set(), []
        if turn_ids:
            async def _fetch():
                async with provider.pool.acquire() as conn:
                    return await conn.fetch(
                        "SELECT id, session_id, content FROM conversations WHERE id = ANY($1::bigint[])",
                        turn_ids,
                    )
            rows = provider._run(_fetch(), timeout=15)
            for row in rows:
                sid = row["session_id"]
                stripped = sid.split(":", 1)[1] if ":" in sid else sid
                session_ids_hit.add(stripped)
                turn_contents_hit.append((stripped, (row["content"] or "")[:120]))

        evidence_sessions, evidence_turns = evidence_ground_truth(q)
        session_recall = (
            len(evidence_sessions & session_ids_hit) / len(evidence_sessions)
            if evidence_sessions else None
        )
        turn_hit_set = set(turn_contents_hit)
        turn_recall = (
            sum(1 for et in evidence_turns if et in turn_hit_set) / len(evidence_turns)
            if evidence_turns else None
        )

        prompt = HISTORY_TEMPLATE.format(injected, q["question_date"], q["question"])
        est_cost = len(prompt) / 4 / 1e6 * 2.0 + GEN_LENGTH / 1e6 * 10.0
        try:
            ledger.check_room(est_cost)
        except SpendCapHit as e:
            print(f"SPEND CAP HIT, stopping cleanly: {e}")
            break
        result = call_openrouter(ANSWER_MODEL, prompt, GEN_LENGTH)
        actual_cost = (result["prompt_tokens"] * 2.0 + result["completion_tokens"] * 10.0) / 1e6
        ledger.record(actual_cost, {"question_id": qid, "condition": args.condition})

        row = {
            "question_id": qid, "condition": args.condition, "model": ANSWER_MODEL,
            "temperature": 0.0, "max_tokens": GEN_LENGTH,
            "hypothesis": result["content"], "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"], "cost_usd": actual_cost,
            "lookup_latency_s": lookup_latency_s,
            "injected_chars": len(injected),
            "injected_tokens_est": len(injected) / 4,
            "injected_sha256": __import__("hashlib").sha256(injected.encode()).hexdigest(),
            "injected_turn_ids": turn_ids,
            "evidence_session_ids": sorted(evidence_sessions),
            "evidence_session_recall": session_recall,
            "evidence_turn_count": len(evidence_turns),
            "evidence_turn_recall": turn_recall,
        }
        append_trace(args.condition, row)
        print(f"  {qid}: lookup {lookup_latency_s*1000:.0f}ms, inj {len(injected)}c, "
              f"sess_recall={session_recall}, turn_recall={turn_recall}, "
              f"${actual_cost:.4f} (ledger ${ledger.total_usd:.4f})", flush=True)

    provider.shutdown()
    if not_yet_ingested:
        print(f"NOTE: {len(not_yet_ingested)} questions not yet ingested, "
              f"re-run this script after ingest catches up.")


if __name__ == "__main__":
    main()
