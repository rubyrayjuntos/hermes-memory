"""Default min_similarity calibration.

2026-11-25 review finding (Q10 in the 10-question recall eval): a stored
refusal ("I can't look up someone's personal info...") scored 0.5648
similarity against "What's my sister's phone number?" -- just over the
old 0.55 floor, so it got surfaced as if it were a real memory. None of
the 7 real ground-truth hits in that same eval scored below 0.5915, so
raising the floor to 0.58 clears the false positive with headroom on
both sides (0.0152 above the leak, 0.0315 below the weakest real hit)
without costing any real hit. See recall_eval_report_v3.json for the
full before/after numbers against all 10 questions.
"""
from hermes_memory.config import HybridAgeConfig


def test_min_similarity_default_is_raised_above_the_q10_false_positive():
    cfg = HybridAgeConfig()
    assert cfg.min_similarity >= 0.58
    # ...but still clears the weakest real hit found in the recall eval
    # (Llama-model question, ground truth at similarity 0.5915) --
    # raising the floor must not cost real recall.
    assert cfg.min_similarity < 0.5915
