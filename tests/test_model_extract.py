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
