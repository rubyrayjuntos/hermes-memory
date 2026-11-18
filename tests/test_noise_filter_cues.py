"""Test noise filter respects implicit first-person cues (T3.6 follow-up)."""
from hermes_memory.turn_filter import _noise_reason


def test_implicit_first_person_not_noise():
    """Short implicit first-person turns are NOT noise."""
    # These are < 50 chars (min_chars default) but have first-person cues
    assert _noise_reason("moved back home to New York last spring", role="user") is None
    assert _noise_reason("Started at Initech in March", role="user") is None
    assert _noise_reason("joined the team last week", role="user") is None


def test_explicit_first_person_not_noise():
    """Existing explicit first-person behavior unchanged."""
    assert _noise_reason("I live in NYC", role="user") is None
    assert _noise_reason("my role is engineer", role="user") is None


def test_short_user_without_cue_is_noise():
    """Short user turns without first-person cues are still noise."""
    # "okay" matches noise_pattern, not short_user
    assert _noise_reason("okay", role="user") is not None
    # These are short and have no cue
    assert _noise_reason("noted", role="user") == "short_user_without_assertion"
    assert _noise_reason("sounds good", role="user") == "short_user_without_assertion"
