"""Backfill links ABOUT edges via AboutConceptLinker composition."""
from hermes_memory.about_concepts import AboutConceptLinker
from hermes_memory.backfill import _make_about_linker
from hermes_memory.provider import HybridAgeMemoryProvider


def test_provider_mro_still_excludes_linker():
    assert AboutConceptLinker not in HybridAgeMemoryProvider.__mro__
    assert not hasattr(HybridAgeMemoryProvider, "_link_turn_concepts")


def test_backfill_linker_is_composed_not_provider():
    linker = _make_about_linker()
    assert type(linker) is AboutConceptLinker
    assert linker._concept_names is None
    assert linker._concept_emb == {}
    assert hasattr(linker, "_link_turn_concepts")
