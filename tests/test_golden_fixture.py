import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "golden_conversation.json"
V18 = ROOT / "sql" / "migrations" / "V18__claims_evidence.sql"


def test_fixture_expected_claims_use_seeded_verbs_and_separate_evidence():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sql = V18.read_text(encoding="utf-8")
    seeded_verbs = set(re.findall(r"\('([^']+)',\s*(?:true|false),", sql))
    ideal_claims = fixture["ideal_claims"]

    for mode in ("rules_only", "rules_then_model"):
        claims = fixture["expected_claims"][mode]
        evidence = fixture["expected_evidence"][mode]
        assert all(claim["verb"] in seeded_verbs for claim in claims)
        assert all("speaker" not in claim for claim in claims)
        assert all("speaker" in item and "source_turn_id" in item for item in evidence)
        assert all(claim in ideal_claims for claim in claims)

    assert len(ideal_claims) == 12
    assert len(ideal_claims) > len(fixture["expected_claims"]["rules_only"])
    assert any(claim["subject"] == "I" and claim["verb"] == "lives_in" for claim in ideal_claims)
    assert {
        "subject": "I",
        "verb": "lives_in",
        "object": "NYC",
        "polarity": "positive",
        "current": False,
        "status": "grounded",
        "event_time": "2024-01-10T12:00:00+00:00",
    } in ideal_claims


def test_fixture_questions_have_typed_expectations():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    required = {
        "id", "kind", "text", "expected_answer", "expect_contains",
        "expect_absent", "expect_claim",
    }
    valid_kinds = {
        "functional_current", "history", "alias", "non_functional", "negation",
        "paste_not_user", "assistant_unconfirmed", "abstain",
    }

    assert fixture["questions"]
    for question in fixture["questions"]:
        assert required <= question.keys()
        assert question["kind"] in valid_kinds
