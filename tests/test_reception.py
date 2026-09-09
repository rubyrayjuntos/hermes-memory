"""Unit tests for reception-side pure functions (aliases, repair, assertions)."""
from src.hermes_memory.reception import (
    classify_repair,
    extract_alias_equations,
    extract_assertions,
    is_deictic_repair,
    normalize_surface,
)


def test_normalize():
    assert normalize_surface("Tokyo Eye") == "tokyo eye"
    assert normalize_surface("tokyo-eye") == "tokyo eye"
    assert normalize_surface("tokyo_eye!") == "tokyo eye"


def test_alias_equations_explicit_only():
    # "Tokyo Eye, also called tokyo-eye" needs NO row: the normalizer already
    # unifies them. Alias rows are for distinct surfaces ("rig" -> canon).
    assert extract_alias_equations("Tokyo Eye, also called tokyo-eye") == []
    assert extract_alias_equations("The rig (aka Nightingale)") == [
        ("nightingale", "the rig")
    ]
    # No equation → no rows. Similarity output must never reach this function.
    assert extract_alias_equations("Tokyo Eye shares MLflow gates") == []


def test_alias_surface_canon_distinct():
    rows = extract_alias_equations("Hermes Librarian aka librarian")
    assert rows == [("librarian", "hermes librarian")]


def test_repair_cues_tight():
    assert classify_repair("a", "No, I meant the Tokyo Eye rig") == "repaired"
    assert classify_repair("a", "that's wrong, not that one") == "repaired"
    assert classify_repair("a", "you don't have to keep saying masterclass") == "repaired"
    assert classify_repair("a", "yes, make sure the repair is in the repo") == "unknown"
    assert classify_repair("a", "no problem, looks good") == "unknown"
    assert classify_repair("a", "I know kung fu") == "unknown"
    assert classify_repair("a", "let's apply first to HPC S2S") == "unknown"


def test_deictic():
    assert is_deictic_repair("no, not that") is True
    assert is_deictic_repair("No, I meant the Tokyo Eye rig") is False


def test_assertions_boring_only():
    claims = extract_assertions("Tokyo Eye uses Poincare geometry.")
    assert claims == [{
        "subject": "Tokyo Eye", "verb": "uses",
        "object": "Poincare geometry",
        "polarity": "positive", "act": "assert",
    }]
    # Lowercase subject → no claim (span stored, unstructured).
    assert extract_assertions("the sun peaked through the clouds") == []
    # No supported verb → no claim, even with entities present.
    assert extract_assertions("Tokyo Eye shares MLflow gates with the factory") == []
