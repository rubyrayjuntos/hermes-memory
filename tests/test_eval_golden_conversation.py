import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scripts.eval_golden_conversation import (
    _build_config,
    _claim_key,
    _drained,
    _event_time_agreement,
    _expected_rows,
    _prf,
    _turn_admission,
    _validate_dsn,
)

FIXTURE = Path(__file__).parent / "fixtures" / "golden_conversation.json"


def test_expected_rows_uses_the_provider_admission_rule_for_both_roles():
    turns = [
        {"role": "user", "content": "I live in NYC."},
        {"role": "assistant", "content": "Noted: you live in NYC."},
        {"role": "user", "content": "What is a good weekly status email format?"},
        {"role": "assistant", "content": "Keep it to three bullets and one ask."},
    ]

    assert _expected_rows(turns) == 2


def test_fixture_admission_lists_dropped_turn_ids_and_reasons():
    turns = json.loads(FIXTURE.read_text(encoding="utf-8"))["turns"]

    expected_rows, dropped = _turn_admission(turns)

    assert expected_rows == 27
    assert len(dropped) == 15
    assert all("turn_id" in item and "role" in item and "reason" in item for item in dropped)
    assert all(item["role"] == "assistant" or item["reason"] == "noise_pattern" for item in dropped)
    assert all(item["reason"] in {"short_non_user", "noise_pattern"} for item in dropped)
    assert all("content" not in item for item in dropped)


def test_drain_requires_all_rows_and_jobs_complete():
    snapshot = {
        "row_count": 4,
        "complete_rows": 4,
        "expected_rows": 4,
        "rules_jobs": 4,
        "rules_done": 4,
        "jobs_pending": 0,
        "jobs_failed": 0,
    }

    assert _drained(**snapshot) is True
    assert _drained(**{**snapshot, "jobs_pending": 1}) is False
    assert _drained(**{**snapshot, "jobs_failed": 1}) is False
    assert _drained(**{**snapshot, "rules_jobs": 3}) is False
    assert _drained(**{**snapshot, "row_count": 5}) is False


def test_precision_recall_f1_are_counted_separately():
    metrics = _prf(["a", "b"], ["a", "c"])

    assert metrics == {
        "expected": 2,
        "actual": 2,
        "matched": 1,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
    }


def test_self_entity_subject_matches_first_person_ideal():
    ideal = _claim_key({
        "subject": "I", "verb": "lives_in", "object": "NYC",
        "polarity": "positive", "current": True,
    })
    stored = _claim_key({
        "subject": "(self)", "verb": "lives_in", "object": "NYC",
        "polarity": "positive", "current": True,
    })
    assert stored == ideal


def test_event_time_agreement_scores_order_not_fixture_wall_clock():
    expected = [
        {
            "source_turn_id": "t1", "subject": "I", "verb": "lives_in", "object": "NYC",
            "speaker": "user", "extractor_version": "1",
        },
        {
            "source_turn_id": "t2", "subject": "Acme", "verb": "uses", "object": "Postgres",
            "speaker": "user", "extractor_version": "1",
        },
    ]
    actual = [
        {
            "span_id": 1, "event_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "subject_surface": "I", "verb": "lives_in", "object_surface": "NYC",
            "speaker": "user", "extractor_version": "1",
        },
        {
            "span_id": 2, "event_time": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "subject_surface": "Acme", "verb": "uses", "object_surface": "Postgres",
            "speaker": "user", "extractor_version": "1",
        },
    ]
    result = _event_time_agreement(
        expected,
        actual,
        {"t1": 1, "t2": 2},
        {
            "t1": {"ts": "2024-01-01T00:00:00+00:00"},
            "t2": {"ts": "2024-02-01T00:00:00+00:00"},
        },
    )

    assert result == {"matched_turns": 2, "compared_pairs": 1, "agreement": 1.0}


def test_golden_runner_refuses_installed_port_and_non_eval_database():
    with pytest.raises(SystemExit, match="port 5450"):
        _validate_dsn("postgres://hermes:***@127.0.0.1:5450/hermes_memory_eval", allow_test_dsn=False)
    with pytest.raises(SystemExit, match="port 5450"):
        _validate_dsn("host=127.0.0.1 port=5450 dbname=hermes_memory_eval", allow_test_dsn=False)
    with pytest.raises(SystemExit, match="must name"):
        _validate_dsn("postgres://hermes:***@127.0.0.1:5453/hermes_memory", allow_test_dsn=False)


def test_golden_runner_allow_test_dsn_permits_port_5450_hermes_test_only():
    # 2026-11-24: allow_test_dsn=True was dead code -- the hard port-5450
    # rejection fired before this flag got a chance to apply, so
    # test_golden_ingest.py's own call with allow_test_dsn=True against the
    # real hermes_test fixture DSN (port 5450) always raised. The
    # allowed = allowed or database == "hermes_test" line a few lines down
    # only makes sense if port 5450 can reach it at all.
    _validate_dsn("postgres://hermes:***@127.0.0.1:5450/hermes_test", allow_test_dsn=True)
    # Still refuses port 5450 for any OTHER database, even with the flag on --
    # the flag is a narrow one-database exception, not a blanket port bypass.
    with pytest.raises(SystemExit, match="port 5450"):
        _validate_dsn("postgres://hermes:***@127.0.0.1:5450/hermes_memory", allow_test_dsn=True)
    # And it changes nothing when the flag is off (same as the test above).
    with pytest.raises(SystemExit, match="port 5450"):
        _validate_dsn("postgres://hermes:***@127.0.0.1:5450/hermes_test", allow_test_dsn=False)


def test_model_mode_requires_an_explicit_endpoint_and_model(monkeypatch):
    monkeypatch.delenv("HYBRID_AGE_EXTRACT_URL", raising=False)
    monkeypatch.delenv("HYBRID_AGE_EXTRACT_MODEL", raising=False)

    with pytest.raises(ValueError, match="requires HYBRID_AGE_EXTRACT_URL"):
        _build_config("postgres://hermes@127.0.0.1:5453/hermes_memory_eval", "rules_then_model")
