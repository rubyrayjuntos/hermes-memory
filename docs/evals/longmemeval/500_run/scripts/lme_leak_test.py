"""Cross-question leak test (per explicit user instruction, before any
generation runs): prefetch() question N's query under question M's
agent_identity and confirm zero injected items genuinely belong to N's
ingested haystack. Tests all 3x2=6 off-diagonal (N,M) pairs among the
first 3 ingested questions.

Uses the SAME shared-embedder-cached provider, re-initialize()'d per call
with the WRONG owner on purpose, exactly mirroring how the real generation
harness will call prefetch() -- if there's a leak, this is where it would
show up.
"""
import json
import sys

sys.path.insert(0, "/home/rswan/Documents/Codex/hermes-memory/src")

from hermes_memory.config import load_config  # noqa: E402
from hermes_memory.provider import HybridAgeMemoryProvider  # noqa: E402

DATASET_PATH = "/home/rswan/Documents/Codex/LongMemEval/data/longmemeval_s_cleaned.json"
CHECKPOINT_PATH = "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_full_ingest_checkpoint.json"


def main():
    dataset = json.load(open(DATASET_PATH))
    by_id = {q["question_id"]: q for q in dataset}
    ckpt = json.load(open(CHECKPOINT_PATH))
    owner_map = ckpt["owner_map"]
    qids = list(ckpt["done"].keys())[:3]
    print(f"Testing cross-question leaks among: {qids}")

    provider = HybridAgeMemoryProvider(config=load_config())
    provider.config.queue_maxsize = 4096

    results = []
    for n_qid in qids:
        n_q = by_id[n_qid]
        n_question_text = n_q["question"]
        n_owner_text_fragments = set()
        # Collect a few distinctive substrings unique to N's haystack so we
        # can detect if M's identity nonetheless surfaces N's content.
        for sess in n_q["haystack_sessions"]:
            for turn in sess:
                c = (turn.get("content") or "")
                if len(c) > 40:
                    n_owner_text_fragments.add(c[:60])
        for m_qid in qids:
            if m_qid == n_qid:
                continue
            m_owner = owner_map[m_qid]
            provider.initialize(session_id=f"leaktest:{m_owner}", agent_identity=m_owner)
            injected = provider.prefetch(n_question_text, session_id=f"leaktest:{m_owner}:q")
            leaked_fragments = [f for f in n_owner_text_fragments if f[:40] in injected]
            results.append({
                "query_from_question": n_qid,
                "run_under_identity_of": m_qid,
                "injected_chars": len(injected),
                "leaked_fragment_count": len(leaked_fragments),
                "injected_preview": injected[:300],
            })
            status = "LEAK" if leaked_fragments else "clean"
            print(
                f"[{status}] query={n_qid} under identity={m_qid}: "
                f"injected {len(injected)} chars, {len(leaked_fragments)} leaked fragments",
                flush=True,
            )

    provider.shutdown()
    json.dump(results, open(
        "/home/rswan/.hermes/profiles/linkibinki/cache/scratch/lme_leak_test_results.json", "w"
    ), indent=2)
    n_leaks = sum(1 for r in results if r["leaked_fragment_count"] > 0)
    print(f"\nTOTAL: {len(results)} pairs tested, {n_leaks} leaked.")


if __name__ == "__main__":
    main()
