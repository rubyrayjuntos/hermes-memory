"""Surface rules for entities (graph layer spec §4.1)."""
import pytest

from hermes_memory.entities import (
    classify_object,
    is_abstained,
    is_first_person,
    normalize_name,
)


@pytest.mark.parametrize("surface", [
    "he", "She", "they", "it", "We", "you", "them", "this", "That", "those",
    "someone", "something", "the project", "The App", "the repo", "the team",
    "the company", "the thing", "the user", "of the", "", "   ", "!!!", "🙂",
])
def test_pronouns_demonstratives_generics_and_empty_abstain(surface):
    assert is_abstained(surface)


@pytest.mark.parametrize("surface", ["Raul", "Acme Corp", "Tokyo Eye", "Postgres", "project Hermes"])
def test_names_do_not_abstain(surface):
    assert not is_abstained(surface)


@pytest.mark.parametrize("surface", ["I", "me", "My", "myself"])
def test_first_person_is_not_abstained_and_is_detected(surface):
    assert is_first_person(surface)
    assert not is_abstained(surface)


def test_normalize_name_matches_reception_normalizer():
    assert normalize_name("  Tokyo-Eye ") == "tokyo eye"


@pytest.mark.parametrize("surface,kind", [
    ("Acme", "entity"),
    ("Acme Corp", "entity"),
    ("Austin", "entity"),
    ("me", "entity"),
    ("a vector database for memory", "literal"),
    ("42", "literal"),
    ("2026-01-01", "literal"),
    ("it", "abstain"),
    ("the project", "abstain"),
])
def test_classify_object(surface, kind):
    assert classify_object(surface) == kind
