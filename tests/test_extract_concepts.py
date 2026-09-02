"""Concept identity: no single-word hubs, slug snap."""
from hermes_memory.provider import SNAP_COSINE, _extract_concepts, _slug, is_one_word_concept


def test_extract_drops_single_word_fallback():
    assert _extract_concepts("Decide. Pulling the branch now.") == []
    assert _extract_concepts("Got it. Perfect.") == []


def test_extract_keeps_multiword_title_case():
    names = _extract_concepts("Tokyo Eye and Hermes Agent are the hubs.")
    assert "Tokyo Eye" in names
    assert "Hermes Agent" in names


def test_slug_normalizes():
    assert _slug("Tokyo Eye") == _slug("tokyo-eye")
    assert _slug("  Hermes Agent ") == "hermes-agent"


def test_snap_threshold():
    assert SNAP_COSINE == 0.85


def test_is_one_word_concept():
    assert is_one_word_concept("Decide") is True
    assert is_one_word_concept("Pulling") is True
    assert is_one_word_concept("Tokyo Eye") is False
    assert is_one_word_concept("Hermes Agent") is False
    assert is_one_word_concept("Zephyr") is False
    assert is_one_word_concept("Atlas") is False
    assert is_one_word_concept("") is False
