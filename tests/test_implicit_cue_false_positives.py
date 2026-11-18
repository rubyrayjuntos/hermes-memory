"""Test implicit first-person cues don't create false-positive claims (T3.6)."""
from hermes_memory.reception import extract_self_assertions


def test_implicit_verbs_no_self_context():
    """Implicit verbs without self-context should NOT produce claims."""
    # These have implicit verbs but no personal context
    assert extract_self_assertions("Left the meeting early") == []
    assert extract_self_assertions("Started the build") == []
    assert extract_self_assertions("Joined the call") == []
    assert extract_self_assertions("moved the cursor to the left") == []


def test_left_verb_especially_risky():
    """'left' is risky — test it doesn't produce spurious works_for claims."""
    # "left" could be confused with past employment
    assert extract_self_assertions("left the room") == []
    assert extract_self_assertions("left early") == []
    assert extract_self_assertions("Left at 5pm") == []
    # Rules don't extract "left <company>" — that's intentional.
    # The implicit verb is only for noise filter + model cue, not rules.
    assert extract_self_assertions("I left Initech last year") == []


def test_golden_junk_still_zero_claims():
    """Golden v2 junk/is-junk strings still produce zero claims after filter change."""
    # From golden_conversation.json "junk_strings" and "is_junk"
    junk = [
        "okay",
        "got it",
        "thanks",
        "sure",
        "yep",
        "nope",
        "> some quoted text",
        "$ ls -la",
        "```python\nprint('hello')\n```",
        "`inline code`",
        "user@host:~$ command",
    ]
    for turn in junk:
        claims = extract_self_assertions(turn)
        assert claims == [], f"Junk turn '{turn[:40]}...' produced claims: {claims}"


def test_valid_implicit_still_extract():
    """Confirm turns with explicit pronouns still extract (regression guard)."""
    # Test patterns that DO extract via rules (not every first-person turn does)
    
    claims = extract_self_assertions("I live in New York")
    assert len(claims) >= 1
    assert any("New York" in c["object"] for c in claims)

    claims = extract_self_assertions("I work at Initech")
    assert len(claims) >= 1
    assert any("Initech" in c["object"] for c in claims)

    claims = extract_self_assertions("my role is engineer")
    assert len(claims) >= 1
    assert any("engineer" in c["object"] for c in claims)
