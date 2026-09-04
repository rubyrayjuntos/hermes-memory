"""P3 — extraction guards; P4 — slug normalization idempotence."""
import pytest

from hermes_memory.extract_nouns import _slug, extract_nouns

pytestmark = pytest.mark.property


def test_p3_no_newline_span_in_multiword():
    labels = [n.label for n in extract_nouns("Ok then Atlas Vault\nnewline Should Not Span")]
    assert not any("\n" in lbl for lbl in labels)


def test_p3_rejects_stopword_led_phrase():
    labels = [n.label.lower() for n in extract_nouns("The Fix is broken")]
    assert "the fix" not in labels


def test_p4_slug_idempotent():
    s = _slug("Tokyo Eye")
    assert _slug(s) == s
    assert _slug("  Hermes Agent ") == _slug("hermes-agent")
