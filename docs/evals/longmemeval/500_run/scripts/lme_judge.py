"""Judge grading: the OFFICIAL LongMemEval judge prompt from
src/evaluation/evaluate_qa.py (commit 9e0b455f, sha256
ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251),
reimplemented verbatim here (get_anscheck_prompt, copy-pasted unchanged)
so it can be routed through OpenRouter instead of a direct OpenAI client.
The prompt TEXT and grading LOGIC are byte-for-byte the official script's;
only the HTTP client/model routing differs.

judge_model = openai/gpt-4o-2024-08-06 via OpenRouter (same model id as
model_zoo['gpt-4o'] in the official script).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/rswan/.hermes/profiles/linkibinki/cache/scratch")

from lme_openrouter_client import JUDGE_MODEL, Ledger, SpendCapHit, call_openrouter  # noqa: E402

DATASET_PATH = "/home/rswan/Documents/Codex/LongMemEval/data/longmemeval_s_cleaned.json"
TRACE_DIR = Path("/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_traces")
JUDGE_DIR = Path("/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_judged")
JUDGE_DIR.mkdir(exist_ok=True)


def get_anscheck_prompt(task, question, answer, response, abstention=False):
    """Verbatim copy of evaluate_qa.py's get_anscheck_prompt -- do not edit
    without re-diffing against the LongMemEval repo at 9e0b455f."""
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        prompt = template.format(question, answer, response)
    return prompt


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", required=True,
                     choices=["no_memory", "full_context", "tiered_off", "tiered_on"])
    args = ap.parse_args()

    dataset = json.load(open(DATASET_PATH))
    qid2q = {q["question_id"]: q for q in dataset}

    hyp_path = TRACE_DIR / f"{args.condition}.jsonl"
    hypotheses = load_jsonl(hyp_path)
    out_path = JUDGE_DIR / f"{args.condition}.jsonl"
    already = {r["question_id"] for r in load_jsonl(out_path)}

    ledger = Ledger("judge", cap_usd=3.0)  # small separate cap; judge calls are tiny
    n_done = 0
    for entry in hypotheses:
        qid = entry["question_id"]
        if qid in already:
            continue
        q = qid2q.get(qid)
        if q is None:
            print(f"WARNING: {qid} not in reference dataset, skipping")
            continue
        qtype = q["question_type"]
        abstention = qid.endswith("_abs")
        prompt = get_anscheck_prompt(qtype, q["question"], q["answer"], entry["hypothesis"],
                                      abstention=abstention)
        est_cost = len(prompt) / 4 / 1e6 * 2.5 + 10 / 1e6 * 10.0
        try:
            ledger.check_room(est_cost)
        except SpendCapHit as e:
            print(f"SPEND CAP HIT, stopping cleanly: {e}")
            break
        result = call_openrouter(JUDGE_MODEL, prompt, max_tokens=10)
        actual_cost = (result["prompt_tokens"] * 2.5 + result["completion_tokens"] * 10.0) / 1e6
        ledger.record(actual_cost, {"question_id": qid, "condition": args.condition})
        label = "yes" in result["content"].lower()
        row = {
            "question_id": qid, "condition": args.condition, "question_type": qtype,
            "abstention": abstention, "judge_model": JUDGE_MODEL,
            "judge_verdict_raw": result["content"], "label_correct": label,
            "cost_usd": actual_cost,
        }
        with open(out_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        n_done += 1
        print(f"  {qid} [{qtype}{'/_abs' if abstention else ''}]: {label} "
              f"(ledger ${ledger.total_usd:.4f})", flush=True)

    print(f"Judged {n_done} new entries for {args.condition} "
          f"({len(already)+n_done}/{len(hypotheses)} total).")


if __name__ == "__main__":
    main()
