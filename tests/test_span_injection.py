"""Unit tests for the provenance-first span renderer (neighbor rule)."""
from hermes_memory.provider_helpers import format_span_injection

USER = {
    "turn_id": 914,
    "role": "user",
    "created_at": "2026-09-08T04:00:00+00:00",
    "content": "Tokyo Eye shares MLflow gates with the factory.",
}
ASSISTANT = {
    "turn_id": 907,
    "role": "assistant",
    "created_at": "2026-09-07T07:00:00+00:00",
    "content": "HPC S2S docs are ready.",
}


def test_empty_injects_nothing():
    assert format_span_injection([]) == ""
    assert format_span_injection([], token_budget=10) == ""


def test_speaker_bins():
    out = format_span_injection([USER, ASSISTANT])
    assert "<grounded>" in out and "<unconfirmed" in out
    assert 'speaker="user"' in out and 'speaker="assistant"' in out
    assert 'uptake="unknown"' in out  # no classifier yet → abstain, not accept


def test_unknown_speaker_quarantined():
    out = format_span_injection([{"turn_id": 1, "content": "mystery"}])
    assert "<grounded>" not in out
    assert "<unconfirmed" in out


def test_neighbors_subordinate_and_capped():
    hit = dict(USER, prev=dict(ASSISTANT), next={"turn_id": 915, "role": "user",
               "content": "x" * 2000, "created_at": "2026-09-08T05:00:00+00:00"})
    out = format_span_injection([hit], token_budget=1200)
    assert out.count('neighbor="true"') == 2
    assert "x" * 301 not in out  # neighbor body capped at 300 chars


def test_hits_pack_before_neighbors():
    hit = dict(USER, prev=dict(ASSISTANT), next=dict(ASSISTANT, turn_id=915))
    # Budget fits the hit (~60 tokens) but not hit + 2 neighbors.
    out = format_span_injection([hit], token_budget=100)
    assert 'id="914"' in out
    assert 'neighbor="true"' not in out


def test_uptake_passthrough():
    span = dict(USER, uptake="accepted")
    out = format_span_injection([span])
    assert 'uptake="accepted"' in out


def test_no_path_verbalization():
    seed_like = dict(USER, paths=[{"triple": "[A] -ABOUT-> [B]"}],
                     best_score=0.89, similarity=0.9)
    out = format_span_injection([seed_like])
    assert "previously linked" not in out
    assert "ABOUT" not in out
