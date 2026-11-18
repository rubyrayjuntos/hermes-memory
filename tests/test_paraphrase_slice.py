import json
from pathlib import Path

from hermes_memory.reception import extract_assertions, extract_self_assertions
from hermes_memory.turn_filter import segment_turn

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "golden_conversation.json"
SLICE = ROOT / "tests" / "fixtures" / "paraphrase_slice.json"


def _authored_claims(text: str) -> list[dict]:
    out: list[dict] = []
    for seg in segment_turn(text):
        if seg.kind != "authored":
            continue
        piece = text[seg.start:seg.end]
        out.extend(extract_assertions(piece))
        out.extend(extract_self_assertions(piece))
    return out


def test_paraphrase_slice_exists_and_keeps_golden_v2():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    slice_ = json.loads(SLICE.read_text(encoding="utf-8"))
    assert golden["fixture_version"] == 2
    assert slice_["name"] == "paraphrase_slice"
    assert slice_["junk_strings"] == golden["junk_strings"]
    assert slice_["is_junk_strings"] == golden["is_junk_strings"]


def test_paraphrase_facts_are_invisible_to_rules():
    slice_ = json.loads(SLICE.read_text(encoding="utf-8"))
    facts = [t for t in slice_["turns"] if t.get("expect_rules_claims") is False and t.get("ideal_claim")]
    assert len(facts) == 3
    for turn in facts:
        assert _authored_claims(turn["content"]) == [], (turn["turn_id"], _authored_claims(turn["content"]))


def test_paraphrase_slice_ideal_claims():
    slice_ = json.loads(SLICE.read_text(encoding="utf-8"))
    assert slice_["ideal_claims"] == [
        {"subject": "I", "verb": "lives_in", "object": "New York", "polarity": "positive", "current": True, "status": "grounded"},
        {"subject": "I", "verb": "works_for", "object": "Initech", "polarity": "positive", "current": True, "status": "grounded"},
        {"subject": "I", "verb": "role_is", "object": "staff engineer", "polarity": "positive", "current": True, "status": "grounded"},
    ]
    assert slice_["expected_claims"]["rules_only"] == []


def test_paraphrase_slice_abstains_on_question_hypothetical_and_paste():
    slice_ = json.loads(SLICE.read_text(encoding="utf-8"))
    abstain = [t for t in slice_["turns"] if t.get("expect_no_claim")]
    assert {t["kind"] for t in abstain} == {"question", "hypothetical", "pasted_doc"}
    for turn in abstain:
        assert _authored_claims(turn["content"]) == [], (turn["turn_id"], _authored_claims(turn["content"]))


def test_paraphrase_slice_junk_produces_nothing():
    slice_ = json.loads(SLICE.read_text(encoding="utf-8"))
    for s in slice_["junk_strings"]:
        for wrap in ("{s}", "I joined {s}.", "I work at {s}."):
            text = wrap.format(s=s)
            assert _authored_claims(text) == [], (s, wrap, _authored_claims(text))
    for text in slice_["is_junk_strings"]:
        assert _authored_claims(text) == [], (text, _authored_claims(text))
