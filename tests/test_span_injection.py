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


def test_accepted_assistant_joins_grounded():
    span = dict(ASSISTANT, uptake="accepted")
    out = format_span_injection([span])
    assert "<grounded>" in out and 'speaker="assistant"' in out
    assert "<unconfirmed" not in out


def test_repaired_prior_becomes_superseded_note():
    span = dict(ASSISTANT, uptake="repaired", retracted=[
        {"subject": "Nightingale", "verb": "uses", "object": "Atlas"}])
    out = format_span_injection([span])
    assert "HPC S2S docs are ready" not in out  # body withheld, not live
    assert "<superseded>" in out
    assert "Nightingale uses Atlas" in out


def test_repaired_without_subjects_still_noted():
    span = dict(ASSISTANT, uptake="repaired")
    out = format_span_injection([span])
    assert "<superseded>" in out and "prior attempt superseded" in out


def test_asserted_spans_outrank_slogans_and_paste():
    slogan = dict(USER, turn_id=1, content="Tokyo Eye shares MLflow gates")
    asserted = dict(USER, turn_id=2, content="Deloitte uses Azure.",
                    live_claims=1)
    paste = dict(USER, turn_id=3,
                 content="[IMPORTANT: Background process proc_1 completed\nok]")
    out = format_span_injection([slogan, paste, asserted])
    assert out.index('id="2"') < out.index('id="1"') < out.index('id="3"')
    assert 'paste="true"' in out
