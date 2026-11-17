from hermes_memory.turn_filter import _is_noise, _noise_reason


def test_short_user_assertions_are_retained():
    assert _is_noise("I live in NYC.", role="user") is False
    assert _is_noise("Role is engineer.", role="user") is False


def test_legacy_and_assistant_short_turn_filtering_is_unchanged():
    assert _is_noise("I live in NYC.") is True
    assert _is_noise("ok") is True
    assert _is_noise("thanks!") is True
    assert _is_noise("Noted.", role="assistant") is True
    assert _is_noise("The team uses Postgres today.", role="assistant") is True


def test_short_user_turn_needs_a_token_bounded_assertion_signal():
    assert _is_noise("Weirdly, the build is slow.", role="user") is True
    assert _is_noise("My project uses Postgres.", role="user") is False


def test_noise_reason_explains_each_admission_rejection():
    assert _noise_reason("  ") == "empty"
    assert _noise_reason("thanks!", role="user") == "noise_pattern"
    assert _noise_reason("Noted: you live in NYC.", role="assistant") == "short_non_user"
    assert _noise_reason("Maybe later.", role="user") == "short_user_without_assertion"
    assert _noise_reason("I live in NYC.", role="user") is None
