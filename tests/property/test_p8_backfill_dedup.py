"""P8 — backfill dedup-key uniqueness + bridge insert/delete symmetry.

Pure-function portion: the dedup key derivation ``(lower(name), label)`` used
when backfilling graph vertices is unique per (name, label) pair. The
derivation under test IS the production one — ``hermes_memory.store.dedup_key``
— so the property suite cannot drift from src.

Bridge insert/delete symmetry requires the DB — the integration-marked twin of
this test lives in tests/integration/test_smoke.py and runs under the
``integration`` marker.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from strategies import real_module_names  # noqa: F401  (suite convention)

from hermes_memory.store import dedup_key  # noqa: E402

LABELS = st.sampled_from([
    "Person", "Project", "Technology", "Organization", "Concept", "Domain",
    "Skill", "Tool", "Repo", "File", "Module", "Dependency",
])


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
