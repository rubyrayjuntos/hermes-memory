from hermes_memory.model_extract import grounded_claims

VERBS = {"uses", "lives_in"}


def test_name_must_appear_in_the_turn_or_context():
    raw = {"claims": [
        {"subject": "Acme", "verb": "uses", "object": "Postgres", "polarity": "positive"},
        {"subject": "Acme", "verb": "uses", "object": "Oracle", "polarity": "positive"},
    ]}
    kept = grounded_claims(raw, "Acme uses Postgres.", ["Earlier we mentioned Postgres."], VERBS)
    assert [c["object"] for c in kept] == ["Postgres"]


def test_unknown_verb_and_pronoun_subject_are_dropped():
    raw = {"claims": [
        {"subject": "Acme", "verb": "likes", "object": "Postgres", "polarity": "positive"},
        {"subject": "it", "verb": "uses", "object": "Postgres", "polarity": "positive"},
    ]}
    assert grounded_claims(raw, "Acme uses Postgres. it uses Postgres.", [], VERBS) == []


def test_invalid_polarity_drops_that_claim_only():
    raw = {"claims": [
        {"subject": "Acme", "verb": "uses", "object": "Oracle", "polarity": "maybe"},
        {"subject": "Acme", "verb": "uses", "object": "Postgres", "polarity": "positive"},
    ]}
    kept = grounded_claims(raw, "Acme uses Postgres and Oracle.", [], VERBS)
    assert [c["object"] for c in kept] == ["Postgres"]
    assert kept[0]["polarity"] == "positive"


def test_literal_i_lives_in_nyc_stays_grounded():
    raw = {"claims": [
        {"subject": "I", "verb": "lives_in", "object": "NYC", "polarity": "positive"},
    ]}
    kept = grounded_claims(raw, "I live in NYC", [], VERBS)
    assert kept == [{"subject": "I", "verb": "lives_in", "object": "NYC", "polarity": "positive"}]


def test_user_aliases_map_to_i_only_with_first_person_cue():
    raw_user = {"claims": [
        {"subject": "user", "verb": "lives_in", "object": "NYC", "polarity": "positive"},
    ]}
    raw_the_user = {"claims": [
        {"subject": "the user", "verb": "lives_in", "object": "NYC", "polarity": "positive"},
    ]}
    raw_me = {"claims": [
        {"subject": "me", "verb": "lives_in", "object": "NYC", "polarity": "positive"},
    ]}
    turn = "I live in NYC"
    for raw in (raw_user, raw_the_user, raw_me):
        kept = grounded_claims(raw, turn, [], VERBS)
        assert kept == [{"subject": "I", "verb": "lives_in", "object": "NYC", "polarity": "positive"}], raw
    dropped = grounded_claims(
        {"claims": [{"subject": "I", "verb": "lives_in", "object": "NYC", "polarity": "positive"}]},
        "moved back home to New York last spring",
        [],
        VERBS,
    )
    assert dropped == []


def test_founded_and_ungrounded_object_are_dropped():
    raw = {"claims": [
        {"subject": "Mira", "verb": "founded", "object": "Northstar Labs", "polarity": "positive"},
        {"subject": "I", "verb": "lives_in", "object": "Denver", "polarity": "positive"},
    ]}
    assert grounded_claims(raw, "I live in NYC", [], VERBS | {"founded"}) == []
    assert grounded_claims(raw, "I live in NYC", [], VERBS) == []
