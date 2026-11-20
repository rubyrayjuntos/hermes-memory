"""Spec 5 T5.6: rule-based summaries for work_item.ask_summary /
outcome_summary / reason.

Gate G3's own precedent applies here too: stay on rules (or the
speaker's own words), no model summarizer, until one wins a bake-off
on a bigger test set -- not attempted in this task. "Rule-based" here
means extractive, not generative: pick whole leading sentences up to
a character budget, never cut mid-word/mid-sentence. That is the only
real difference from plain truncation of pasted text, which the spec
explicitly rules out ("not pasted text").

This module never sees pasted segments (code/logs/tables/quotes) at
all -- callers (_run_work) filter to segment_turn's "authored" kind
before calling extractive_summary; see
tests/integration/test_run_work_e2e.py's
test_memory_block_is_stripped_before_extraction and
test_pasted_segment_never_reaches_the_summary for that upstream
guarantee.
"""
from __future__ import annotations

import re

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def extractive_summary(text: str, max_chars: int) -> str:
    """Whole leading sentences, collapsed whitespace, up to max_chars.
    Never cuts mid-sentence when more than one sentence fits; never
    cuts mid-word even when a single sentence must be truncated."""
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if not normalized:
        return ""
    if len(normalized) <= max_chars:
        return normalized

    sentences = _SENTENCE_SPLIT_RE.split(normalized)
    kept = ""
    for sentence in sentences:
        candidate = f"{kept} {sentence}".strip() if kept else sentence
        if len(candidate) > max_chars:
            break
        kept = candidate

    if kept:
        return kept

    # Not even the first sentence fits -- truncate at a word boundary
    # and mark it, rather than slicing a word in half.
    first_sentence = sentences[0]
    budget = max_chars - 3  # reserve room for "..."
    if budget <= 0:
        return normalized[:max_chars]
    truncated = first_sentence[:budget]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."
