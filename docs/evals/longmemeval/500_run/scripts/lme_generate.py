"""Generation harness for all 4 LongMemEval conditions, sharing one answer
model/prompt family and one resumable-per-question-and-condition JSONL
trace file per condition.

Conditions:
  no_memory    -- official run_generation.py's 'no-retrieval' template,
                  same idea reimplemented here to share one script/ledger
                  (the prompt text itself is copied verbatim from
                  src/generation/run_generation.py, unmodified).
  full_context -- entire raw haystack, unretrieved, straight into the
                  prompt (same template family, 'orig-session'-equivalent).
                  Runs ONLY on the fixed stratified 50-question subset
                  (lme_fullcontext_50_subset.json), per explicit budget gate.
  tiered_off   -- hermes-memory's own prefetch(), config.tiered_injection=False.
  tiered_on    -- hermes-memory's own prefetch(), config.tiered_injection=True.
                  Both require the question's owner to already be ingested
                  (checked against the ingest checkpoint); skips anything
                  not yet landed and reports what's pending.

Resumable: each condition's trace file is JSONL, one line per completed
question; a restart reads already-done question_ids and skips them. Spend
is tracked in lme_ledgers/<condition>.json via lme_openrouter_client.Ledger
and hard-capped; hitting the cap stops the run cleanly (no partial/garbled
output), leaving everything already written intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/rswan/Documents/Codex/hermes-memory/src")
sys.path.insert(0, "/home/rswan/.hermes/profiles/linkibinki/cache/scratch")

from lme_openrouter_client import (  # noqa: E402
    ANSWER_MODEL,
    Ledger,
    SpendCapHit,
    call_openrouter,
)

DATASET_PATH = "/home/rswan/Documents/Codex/LongMemEval/data/longmemeval_s_cleaned.json"
INGEST_CHECKPOINT = "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_full_ingest_checkpoint.json"
SUBSET_50_PATH = "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_fullcontext_50_subset.json"
TRACE_DIR = Path("/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_traces")
TRACE_DIR.mkdir(exist_ok=True)

GEN_LENGTH = 500  # matches run_generation.py's non-CoT default

NO_RETRIEVAL_TEMPLATE = "{}"
HISTORY_TEMPLATE = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history.\n\n\n"
    "History Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer:"
)


def load_dataset() -> list[dict]:
    return json.load(open(DATASET_PATH))


def load_trace(condition: str) -> dict[str, dict]:
    path = TRACE_DIR / f"{condition}.jsonl"
    done = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            done[row["question_id"]] = row
    return done


def append_trace(condition: str, row: dict) -> None:
    path = TRACE_DIR / f"{condition}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def build_full_context_prompt(q: dict) -> str:
    history_string = ""
    for i, (date, sess) in enumerate(zip(q["haystack_dates"], q["haystack_sessions"]), start=1):
        sess_string = ""
        for turn in sess:
            c = (turn.get("content") or "").strip()
            sess_string += f"\n\n{turn['role']}: {c}"
        history_string += f"\n### Session {i}:\nSession Date: {date}\nSession Content:\n{sess_string}\n"
    return HISTORY_TEMPLATE.format(history_string, q["question_date"], q["question"])


def run_no_memory(ledger: Ledger, question_ids: list[str] | None, dataset_by_id: dict):
    done = load_trace("no_memory")
    targets = question_ids or list(dataset_by_id.keys())
    remaining = [qid for qid in targets if qid not in done]
    print(f"no_memory: {len(done)} done, {len(remaining)} remaining", flush=True)
    for qid in remaining:
        q = dataset_by_id[qid]
        prompt = NO_RETRIEVAL_TEMPLATE.format(q["question"])
        est_cost = len(prompt) / 4 / 1e6 * 2.0 + GEN_LENGTH / 1e6 * 10.0
        try:
            ledger.check_room(est_cost)
        except SpendCapHit as e:
            print(f"SPEND CAP HIT, stopping cleanly: {e}")
            return
        result = call_openrouter(ANSWER_MODEL, prompt, GEN_LENGTH)
        actual_cost = (result["prompt_tokens"] * 2.0 + result["completion_tokens"] * 10.0) / 1e6
        ledger.record(actual_cost, {"question_id": qid, "condition": "no_memory"})
        row = {
            "question_id": qid, "condition": "no_memory", "model": ANSWER_MODEL,
            "temperature": 0.0, "max_tokens": GEN_LENGTH,
            "hypothesis": result["content"], "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"], "cost_usd": actual_cost,
        }
        append_trace("no_memory", row)
        print(f"  {qid}: {result['prompt_tokens']}pt/{result['completion_tokens']}ct "
              f"${actual_cost:.4f} (ledger total ${ledger.total_usd:.4f})", flush=True)


def run_full_context(ledger: Ledger, subset_ids: list[str], dataset_by_id: dict):
    done = load_trace("full_context")
    remaining = [qid for qid in subset_ids if qid not in done]
    print(f"full_context: {len(done)} done, {len(remaining)} remaining (of 50-subset)", flush=True)
    for qid in remaining:
        q = dataset_by_id[qid]
        prompt = build_full_context_prompt(q)
        est_input_tok = len(prompt) / 4
        est_cost = est_input_tok / 1e6 * 2.0 + GEN_LENGTH / 1e6 * 10.0
        try:
            ledger.check_room(est_cost)
        except SpendCapHit as e:
            print(f"SPEND CAP HIT, stopping cleanly: {e}")
            return
        result = call_openrouter(ANSWER_MODEL, prompt, GEN_LENGTH)
        actual_cost = (result["prompt_tokens"] * 2.0 + result["completion_tokens"] * 10.0) / 1e6
        ledger.record(actual_cost, {"question_id": qid, "condition": "full_context"})
        row = {
            "question_id": qid, "condition": "full_context", "model": ANSWER_MODEL,
            "temperature": 0.0, "max_tokens": GEN_LENGTH,
            "hypothesis": result["content"], "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"], "cost_usd": actual_cost,
        }
        append_trace("full_context", row)
        print(f"  {qid}: {result['prompt_tokens']}pt/{result['completion_tokens']}ct "
              f"${actual_cost:.4f} (ledger total ${ledger.total_usd:.4f})", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", required=True,
                     choices=["no_memory", "full_context", "tiered_off", "tiered_on"])
    args = ap.parse_args()

    dataset = load_dataset()
    dataset_by_id = {q["question_id"]: q for q in dataset}

    if args.condition == "no_memory":
        ledger = Ledger("main_three", cap_usd=15.0)
        run_no_memory(ledger, None, dataset_by_id)
    elif args.condition == "full_context":
        subset = json.load(open(SUBSET_50_PATH))["question_ids"]
        ledger = Ledger("full_context", cap_usd=12.0)
        run_full_context(ledger, subset, dataset_by_id)
    else:
        print(f"{args.condition}: use lme_generate_tiered.py instead (needs the memory provider)")
        sys.exit(1)


if __name__ == "__main__":
    main()
