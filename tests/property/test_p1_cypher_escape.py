"""P1 — Cypher string escaping (docs/plans/v0.1.md §6).

age_str output must contain no unescaped ``'`` or ``\\`` from the input, so
arbitrary text can never break out of a Cypher string literal.
"""
from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strategies import adversarial_text  # noqa: E402

from hermes_memory.store import age_str  # noqa: E402


def _has_unescaped_quote(literal_body: str) -> bool:
    """True if a ``'`` sits after an even (i.e. non-escaping) backslash run."""
    i = 0
    while i < len(literal_body):
        c = literal_body[i]
        if c == "\\":
            run = 0
            while i < len(literal_body) and literal_body[i] == "\\":
                run += 1
                i += 1
            # an even run closes itself; an odd run escapes whatever follows
            escaping = run % 2 == 1
            if escaping and i < len(literal_body):
                i += 1  # escaped char consumed
            continue
        i += 1
        if c == "'":
            return True  # quote reached without an active escape
    return False


@settings(max_examples=500)
@given(adversarial_text)
def test_p1_no_unescaped_quote_or_backslash(value):
    out = age_str(value)
    assert out.startswith("'") and out.endswith("'")
    assert not _has_unescaped_quote(out[1:-1]), \
        f"injection survived for {value!r}: {out!r}"


@settings(max_examples=200)
@given(adversarial_text)
def test_p1_none_renders_null(value):
    assert age_str(None) == "null"
