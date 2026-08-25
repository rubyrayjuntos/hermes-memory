"""Shared Hypothesis strategies for the hermes-memory test suite.

Register: docs/plans/v0.1.md §6 (P1-P8).
"""
from __future__ import annotations

import string

from hypothesis import strategies as st

# -- Adversarial text (P1/P2): quotes, backslashes, newlines, unicode ---------

_ADVERSARIAL_CHARS = (
    "'\"\\"            # Cypher/SQL metacharacters
    "%_\n\r\t"         # LIKE wildcards + control chars
    + string.printable
    + "é漢字🎉"        # unicode
)
# de-duplicate while keeping membership
_ADVERSARIAL_CHARS = "".join(dict.fromkeys(_ADVERSARIAL_CHARS))

adversarial_text = st.text(alphabet=_ADVERSARIAL_CHARS, max_size=200)
"""Arbitrary text heavy in quotes/backslashes/control characters/unicode."""

any_text = st.text(max_size=300)

# -- Safe identifiers (P2 keys, labels) ---------------------------------------

safe_identifier = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,30}", fullmatch=True)

unsafe_identifier = st.text(
    alphabet=st.sampled_from(list("0123456789 -.;'\"\\(){}[]$@#\n\t")),
    min_size=1,
    max_size=20,
).filter(lambda s: not s[0].isalpha() and s[0] != "_")

# -- Property maps (P2) --------------------------------------------------------

prop_values = st.one_of(
    st.none(),
    adversarial_text,
    st.integers(min_value=-(2**40), max_value=2**40),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
)

property_maps = st.dictionaries(safe_identifier, prop_values, max_size=8)


@st.composite
def dicts_with_unsafe_keys(draw):
    """Property maps mixing valid keys with injection-shaped keys."""
    n_safe = draw(st.integers(min_value=0, max_value=4))
    safe = draw(
        st.dictionaries(safe_identifier, prop_values, min_size=n_safe, max_size=n_safe)
    )
    bad_key = draw(st.sampled_from([
        "name') DROP", "a b", "1evil", "k;ey", "ke\"y", "", "key\\",
        "key-x", "key.x", "UNION SELECT",
    ]))
    safe[bad_key] = draw(prop_values)
    return safe


# -- Module names (P5) ---------------------------------------------------------

module_artifacts = st.sampled_from([
    # hex blobs
    "deadbeef", "DEADBEEF01", "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
    # uuids
    "550e8400-e29b-41d4-a716-446655440000",
    "550E8400E29B41D4A716446655440000",
    "550e8400e29b41d4a716446655440000",
    # long digit runs
    "123456", "17092345678901",
    # skip dirs / storage noise
    "src", "lib", "app", "__pycache__", "node_modules", "dist", "build",
    "target", ".git", ".venv", "venv", "site-packages", "cache", "tmp",
    "artifacts", "mlruns", "checkpoints", "logs", "output", "tests",
    "fixtures", "..", ".", "",
])

real_module_names = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_-]{1,29}", fullmatch=True).filter(
    lambda s: not _looks_like_artifact(s)
)


def _looks_like_artifact(name: str) -> bool:
    import re

    return bool(
        re.match(r"^[0-9a-f]{8,}$", name, re.I)
        or re.match(r"^\d{6,}$", name)
        or name.lower() in {
            "src", "lib", "app", ".", "..", "__pycache__", "node_modules",
            "dist", "build", "target", ".git", ".venv", "venv",
            "site-packages", "cache", ".cache", "tmp", ".tmp", "artifacts",
            "mlruns", "mlflow-artifacts", "checkpoints", "logs", "output",
            "outputs", "tests", "test", "fixtures",
        }
    )


# -- Dependency names (P6) -----------------------------------------------------

python_import_names = st.sampled_from([
    "os", "sys", "json", "asyncio", "logging", "re",
    "numpy.core", "pandas.io", "hermes_memory.store",
])

ts_module_names = st.sampled_from([
    "react", "lodash", "./utils/helpers", "../config", "@/components/Button",
    "express", "axios",
])

md_link_targets = st.sampled_from([
    "docs/architecture.md", "../README.md", "src/server.ts", "app.py",
])


def python_source(deps=python_import_names):
    """Python source text importing 1-5 of the sampled modules."""
    return st.builds(
        lambda ds: "\n".join(f"import {d}" for d in ds),
        st.lists(python_import_names, min_size=1, max_size=5, unique=True),
    )
