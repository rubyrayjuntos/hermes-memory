"""Test role name normalization for scoring (T3.6 follow-up)."""
from scripts.spec3_bakeoff import _claim_key, _norm_role


def test_norm_role_exact_match():
    """Exact role names stay unchanged."""
    assert _norm_role("staff engineer") == "staff engineer"
    assert _norm_role("senior developer") == "senior developer"
    assert _norm_role("product manager") == "product manager"


def test_norm_role_gerund_to_noun():
    """'staff engineering' normalizes to 'staff engineer'."""
    assert _norm_role("staff engineering") == "staff engineer"
    assert _norm_role("software engineering") == "software engineer"
    assert _norm_role("product management") == "product manager"


def test_norm_role_preserves_non_roles():
    """Non-role objects stay unchanged (but lowercased)."""
    assert _norm_role("NYC") == "nyc"
    assert _norm_role("Initech") == "initech"
    assert _norm_role("Postgres") == "postgres"


def test_claim_key_uses_role_normalization():
    """Claim keys should normalize role-like objects."""
    claim_a = {
        "subject": "I",
        "verb": "role_is",
        "object": "staff engineer",
        "polarity": "positive",
        "current": True,
    }
    claim_b = {
        "subject": "I",
        "verb": "role_is",
        "object": "staff engineering",
        "polarity": "positive",
        "current": True,
    }

    # These should produce the same key after normalization
    assert _claim_key(claim_a) == _claim_key(claim_b)
