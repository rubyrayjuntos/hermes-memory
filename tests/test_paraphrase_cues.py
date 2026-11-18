"""Test first-person cue expansion for paraphrase slice (T3.6 follow-up)."""
from hermes_memory.model_extract import _has_first_person_cue


def test_explicit_first_person_cues():
    """Existing explicit cues still work."""
    assert _has_first_person_cue("I live in NYC")
    assert _has_first_person_cue("My role is staff engineer")
    assert _has_first_person_cue("these days I'm doing staff engineering")
    assert _has_first_person_cue("Give me a summary")


def test_implicit_first_person_paraphrases():
    """Paraphrases like 'moved to...' or 'Started at...' are first-person."""
    # p01: "moved back home to New York last spring"
    assert _has_first_person_cue("moved back home to New York last spring")
    assert _has_first_person_cue("Moved to Denver last year")
    
    # p03: "Started at Initech in March"
    assert _has_first_person_cue("Started at Initech in March")
    assert _has_first_person_cue("started working at Acme")
    
    # Other common implicit patterns
    assert _has_first_person_cue("Left Acme last month")
    assert _has_first_person_cue("Joined the team in 2024")


def test_non_first_person():
    """Third-person should not match. Questions/hypotheticals still have cues."""
    # p07: question - has "I", so cue matches (question filtering is separate)
    assert _has_first_person_cue("Where do I live these days?")
    
    # p09: hypothetical - has "I", so cue matches (hypothetical filtering is separate)
    assert _has_first_person_cue("if I moved to Denver I'd need a winter coat")
    
    # Third-person - no first-person cues
    assert not _has_first_person_cue("She moved to Boston")
    assert not _has_first_person_cue("The team started the project")
    assert not _has_first_person_cue("Denver is a city")
