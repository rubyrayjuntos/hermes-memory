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

    assert len(ideal_claims) == 11
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
    acme_jobs = [
        claim for claim in ideal_claims
        if claim["subject"] == "I" and claim["verb"] == "works_for" and claim["object"] == "Acme"
    ]
    assert acme_jobs == [{
        "subject": "I",
        "verb": "works_for",
        "object": "Acme",
        "polarity": "positive",
        "current": False,
        "status": "grounded",
        "event_time": "2024-09-01T10:00:00+00:00",
    }]
    assert not any(claim["verb"] == "works_for" and claim["current"] for claim in ideal_claims)


def _authored_claims(text: str) -> list[dict]:
    from hermes_memory.reception import extract_assertions, extract_self_assertions
    from hermes_memory.turn_filter import segment_turn

    out: list[dict] = []
    for seg in segment_turn(text):
        if seg.kind != "authored":
            continue
        piece = text[seg.start:seg.end]
        out.extend(extract_assertions(piece))
        out.extend(extract_self_assertions(piece))
    return out


def test_junk_strings_produce_no_claims():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    junk = fixture["junk_strings"]
    assert junk == [
        "working",
        "a call`",
        "Acme` → `works_for Acme` polarity `positive`",
        "staff engineer\"` and `\"I'm tired\"` unchanged",
        "Acme anymore\"` → **negative**",
        "Acme anymore\"",
    ]
    wraps = (
        "{s}",
        "I joined {s}.",
        "I work at {s}.",
        "I'm a {s}.",
        "Verify Docker is {s}.",
    )
    for s in junk:
        for wrap in wraps:
            text = wrap.format(s=s)
            assert _authored_claims(text) == [], (s, wrap, _authored_claims(text))


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
