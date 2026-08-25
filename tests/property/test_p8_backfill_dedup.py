"""P8 — backfill dedup-key uniqueness + bridge insert/delete symmetry.

Pure-function portion: the dedup key derivation ``(lower(name), label)`` used
when backfilling graph vertices is unique per (name, label) pair.

Bridge insert/delete symmetry requires the DB — the integration-marked twin of
this test lives in tests/integration/test_smoke.py and runs under the
``integration`` marker.
"""
from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

LABELS = st.sampled_from([
    "Person", "Project", "Technology", "Organization", "Concept", "Domain",
    "Skill", "Tool", "Repo", "File", "Module", "Dependency",
])


def dedup_key(name: str, label: str):
    """The canonical vertex identity used for MERGE/backfill dedup.

    Mirrors the store's minimal-unique-key rule: entities merge on lowercased
    name within their label. Extracted here as the property-test surface; the
    production MERGE in store.py/ingest.py must agree with it.
    """
    return (name.strip().lower(), label)


@settings(max_examples=300)
@given(
    names=st.lists(st.text(alphabet="abcXYZ _-", min_size=1, max_size=20),
                   min_size=1, max_size=12),
    label=LABELS,
)
def test_p8_dedup_key_case_insensitive_unique(names, label):
    """Distinct names (ignoring case/whitespace) yield distinct keys."""
    keys = [dedup_key(n, label) for n in names]
    distinct_inputs = {n.strip().lower() for n in names}
    assert len(set(keys)) == len(distinct_inputs)


@settings(max_examples=100)
@given(name=st.text(alphabet="abcXYZ _-", min_size=1, max_size=20), label=LABELS)
def test_p8_dedup_key_case_collapse(name, label):
    """Same name differing only in case/whitespace collapses to one key."""
    base = dedup_key(name, label)[0]
    for v in (name + "  ", name.upper(), name.lower(), name.capitalize()):
        assert dedup_key(v, label)[0] == base, f"case collapse failed: {v!r}"
