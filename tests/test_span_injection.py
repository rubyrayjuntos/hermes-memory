"""Unit tests for the provenance-first span renderer (neighbor rule)."""
from hermes_memory.provider_helpers import format_facts, format_span_injection
from hermes_memory.tokens import count_tokens

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


def test_tier1_full_span_content_is_redacted_not_just_escaped():
    # 2026-11-25: a real Gmail password leaked through exactly this path --
    # _clean_excerpt html-escaped the body (for fence-injection safety) but
    # never ran secret/PII redaction, so a full Tier-1 span carrying a
    # credential or a phone number went out verbatim. Redaction belongs at
    # render time here, same bar as the Tier-2 pointer summary already has.
    span = dict(USER, content=(
        "You gave the persona a Gmail account, with password "
        "**Fake9Secret1!**, and the backup contact is 832-468-4304."
    ))
    out = format_span_injection([span])
    assert "Fake9Secret1!" not in out
    assert "832-468-4304" not in out
    assert "[REDACTED]" in out
    assert "Gmail account" in out  # redaction, not wholesale suppression


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


def test_similarity_score_outranks_paste_and_noclaim_flags_when_present():
    # 2026-11-25 review finding: the first recall eval measured 3 of 4
    # misses where the TOP vector-similarity match got dropped by the
    # token budget anyway, because _rank sorted purely on (paste,
    # noclaim) with no regard for the actual similarity score. Within a
    # bin, similarity must be the PRIMARY sort key -- paste/noclaim only
    # break ties between items with the same (or no) score.
    top_match_but_paste = dict(
        USER, turn_id=1, score=0.91,
        content="[IMPORTANT: Background process proc_1 completed\nthe actual answer]",
    )
    weaker_match_not_paste = dict(
        USER, turn_id=2, score=0.60, content="a vaguely related aside",
    )
    # A tight budget that fits only ONE of the two real hits (each ~20-30
    # tokens) -- the higher-similarity one must be the survivor, even
    # though it is flagged paste and the weaker one isn't.
    out = format_span_injection(
        [weaker_match_not_paste, top_match_but_paste], token_budget=60,
    )
    assert 'id="1"' in out
    assert 'id="2"' not in out


def test_similarity_tie_falls_back_to_paste_and_noclaim_as_before():
    # No score supplied at all (same shape the pre-existing asserted/
    # slogan/paste test uses) -- every item ties on similarity, so the
    # paste/noclaim tiebreak decides exactly as before. This pins that
    # the fix is additive, not a behavior change when scores are absent.
    slogan = dict(USER, turn_id=1, content="Tokyo Eye shares MLflow gates")
    asserted = dict(USER, turn_id=2, content="Deloitte uses Azure.",
                    live_claims=1)
    paste = dict(USER, turn_id=3,
                 content="[IMPORTANT: Background process proc_1 completed\nok]")
    out = format_span_injection([slogan, paste, asserted])
    assert out.index('id="2"') < out.index('id="1"') < out.index('id="3"')


def test_strong_unconfirmed_match_reserves_budget_ahead_of_weak_grounded():
    # 2026-11-25 review finding (Q1 in the 10-question recall eval): the
    # grounded bucket is ALWAYS packed in full before unconfirmed gets a
    # turn, so a top-similarity unconfirmed hit can still lose to a whole
    # bucket of weaker grounded spans that happened to arrive first. A
    # slice of the budget (RESERVE_FRACTION) is now reserved up front for
    # the single strongest match across BOTH buckets, before the normal
    # bucket-priority pass runs on what's left.
    strong_unconfirmed = dict(
        turn_id=1, speaker="assistant", uptake="unknown", score=0.90,
        content="The Nous Portal credential is in auth.json, matches both profiles, confirmed working.",
    )
    weak_grounded = [
        dict(USER, turn_id=2, score=0.56,
             content="a vaguely related note about something else entirely here"),
        dict(USER, turn_id=3, score=0.55,
             content="another loosely related aside about a different unrelated topic"),
        dict(USER, turn_id=4, score=0.55,
             content="yet another weak match about something tangential and unrelated"),
    ]
    out = format_span_injection(
        [*weak_grounded, strong_unconfirmed], token_budget=200,
    )
    assert 'id="1"' in out, "top-similarity match must survive even though it's unconfirmed"


def test_facts_require_grounding_and_source_spans():
    out = format_facts([
        {
            "claim_id": 7,
            "status": "grounded",
            "fact_text": "Acme uses Postgres.",
            "supporting_turns": [{"span_id": 914, "role": "user", "content": "Acme uses Postgres."}],
        },
        {
            "claim_id": 8,
            "status": "unconfirmed",
            "fact_text": "Acme uses Redis.",
            "supporting_turns": [{"span_id": 915, "role": "assistant", "content": "Acme uses Redis."}],
        },
        {
            "claim_id": 9,
            "status": "grounded",
            "fact_text": "Missing provenance",
            "supporting_turns": [],
        },
    ])

    assert '<fact id="7" spans="914"' in out
    assert '<span id="914" speaker="user">Acme uses Postgres.</span>' in out
    assert "Acme uses Redis" not in out
    assert "Missing provenance" not in out


def test_facts_escape_markup_and_enforce_complete_fact_budget():
    out = format_facts([{
        "claim_id": '7"><script>',
        "status": "grounded",
        "fact_text": "<b>" + ("Long fact. " * 400),
        "supporting_turns": [{"span_id": 914, "role": "user", "content": "source"}],
    }], fact_token_budget=80, total_token_budget=120)

    fact_block = out.split("<supporting_turns>", 1)[0].strip()
    assert "&lt;script&gt;" in out
    assert "<b>" not in out
    assert count_tokens(fact_block) <= 80
    assert count_tokens(out) <= 120


def test_thirty_long_grounded_facts_stay_within_400_and_max_tokens():
    facts = []
    filler = "word " * 40
    for i in range(30):
        body = f"Org{i} uses Tool{i}."
        facts.append({
            "claim_id": i + 1,
            "status": "grounded",
            "fact_text": body,
            "supporting_turns": [{
                "span_id": 1000 + i,
                "role": "user",
                "content": f"{body} {filler}",
            }],
        })
    out = format_facts(facts, fact_token_budget=400, total_token_budget=1200, max_facts=30)
    assert out
    fact_block = out.split("<supporting_turns>", 1)[0].strip()
    assert count_tokens(fact_block) <= 400
    assert count_tokens(out) <= 1200
    assert fact_block.startswith("<grounded_facts")
