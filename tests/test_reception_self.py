"""First-person functional rules (Spec 2)."""
from hermes_memory.reception import extract_self_assertions


def test_i_live_in_nyc():
    assert extract_self_assertions("I live in NYC.") == [{
        "subject": "I",
        "verb": "lives_in",
        "object": "NYC",
        "polarity": "positive",
    }]


def test_moved_to_strips_relative_time_not_parsed():
    claims = extract_self_assertions("I moved to Austin last week.")
    assert claims == [{
        "subject": "I",
        "verb": "lives_in",
        "object": "Austin",
        "polarity": "positive",
    }]


def test_im_back_in_nyc():
    assert extract_self_assertions("I'm back in NYC.") == [{
        "subject": "I",
        "verb": "lives_in",
        "object": "NYC",
        "polarity": "positive",
    }]


def test_relocated_to_is_lives_in():
    assert extract_self_assertions("I relocated to Denver.") == [{
        "subject": "I",
        "verb": "lives_in",
        "object": "Denver",
        "polarity": "positive",
    }]


def test_works_at_for_joined():
    expected = [{
        "subject": "I",
        "verb": "works_for",
        "object": "Acme",
        "polarity": "positive",
    }]
    assert extract_self_assertions("I work at Acme") == expected
    assert extract_self_assertions("I work for Acme") == expected
    assert extract_self_assertions("I joined Acme") == expected


def test_role_is_staff_engineer():
    expected = [{
        "subject": "I",
        "verb": "role_is",
        "object": "staff engineer",
        "polarity": "positive",
    }]
    assert extract_self_assertions("My role is staff engineer") == expected
    assert extract_self_assertions("I'm a staff engineer") == expected


def test_role_is_abstains_stative():
    assert extract_self_assertions("I'm tired") == []
    assert extract_self_assertions("I'm not sure") == []


def test_based_in_is_separate_slot():
    assert extract_self_assertions("I'm based in Seattle.") == [{
        "subject": "I",
        "verb": "based_in",
        "object": "Seattle",
        "polarity": "positive",
    }]
    # Does not also emit lives_in.
    verbs = {c["verb"] for c in extract_self_assertions("I'm based in Seattle.")}
    assert verbs == {"based_in"}


def test_dont_work_at_anymore_is_negative():
    assert extract_self_assertions("I don't work at Acme anymore") == [{
        "subject": "I",
        "verb": "works_for",
        "object": "Acme",
        "polarity": "negative",
    }]


def test_abstain_questions_hypotheticals_reported_speech():
    assert extract_self_assertions("Do I live in NYC?") == []
    assert extract_self_assertions("If I lived in Austin…") == []
    assert extract_self_assertions("She said I work at Acme") == []


def test_home_is_nyc_stays_nonfunctional_is():
    from hermes_memory.reception import extract_assertions

    claims = extract_assertions("Home is NYC")
    assert claims == [{
        "subject": "Home",
        "verb": "is",
        "object": "NYC",
        "polarity": "positive",
        "act": "assert",
    }]
    assert extract_self_assertions("Home is NYC") == []


def test_left_quit_need_named_transition():
    assert extract_self_assertions("I left Acme") == []
    assert extract_self_assertions("I quit") == []
    assert extract_self_assertions("I left NYC for Austin") == [{
        "subject": "I",
        "verb": "lives_in",
        "object": "Austin",
        "polarity": "positive",
    }]
    assert extract_self_assertions("I quit Acme to join Globex") == [{
        "subject": "I",
        "verb": "works_for",
        "object": "Globex",
        "polarity": "positive",
    }]


def test_reject_object_over_six_tokens():
    assert extract_self_assertions(
        "I live in the small town just north of the river"
    ) == []


def test_format_facts_renders_self_as_you():
    from hermes_memory.provider_helpers import format_facts

    out = format_facts([{
        "claim_id": 1,
        "status": "grounded",
        "fact_text": "(self) lives in NYC",
        "supporting_turns": [{"span_id": 10, "role": "user", "content": "I live in NYC."}],
    }])
    assert "(self)" not in out
    assert "You lives in NYC" in out or "You live in NYC" in out


async def test_reception_stage_records_self_lives_in():
    from tests.test_reception_stage import FakeStore, make_provider

    store = FakeStore(prior=None)
    await make_provider()._reception_stage(store, 11, "s", "user", "I live in NYC.")
    assert store.claim_inserts
    claims = store.claim_inserts[0][1]
    assert any(
        c["subject"] == "I" and c["verb"] == "lives_in" and c["object"] == "NYC"
        for c in claims
    )
