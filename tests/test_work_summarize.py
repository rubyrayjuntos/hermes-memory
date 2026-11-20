"""Spec 5 T5.6: ask_summary/outcome_summary are short RULE-based
summaries, not pasted text and not a model's -- gate G3's own
precedent (rules_only stays default; no model becomes opt-in without
winning a bake-off on a bigger test set) applies here too: a model
summarizer is explicitly not built in this task.

Extractive, not generative: picks whole leading sentences up to a
character budget rather than cutting mid-word/mid-sentence, which is
the only thing distinguishing this from plain truncation of pasted
text. Pasted segments (code/logs/tables/quotes -- Spec 1's
segment_turn "pasted" kind) are excluded upstream, before this
function ever sees the text (see _run_work's authored_by_span filter
in test_run_work_e2e.py's test_memory_block_is_stripped_before_extraction
and the dedicated pasted-segment test in this file's integration
counterpart).
"""
from __future__ import annotations

from hermes_memory.work_summarize import extractive_summary


def test_short_text_returned_as_is():
    assert extractive_summary("Fixed the bug.", max_chars=300) == "Fixed the bug."


def test_whole_sentences_kept_up_to_the_limit():
    text = "I fixed the auth bug. I also added a test for it. Then I ran the full suite."
    result = extractive_summary(text, max_chars=45)
    # Must contain the first whole sentence, never a partial one.
    assert result.startswith("I fixed the auth bug.")
    # Never cuts mid-sentence: the result ends at a sentence boundary
    # (or is the whole text) -- never an arbitrary character offset.
    assert result.endswith((".", "!", "?"))
    assert len(result) <= 45


def test_a_single_sentence_longer_than_the_budget_truncates_at_a_word_boundary():
    text = "This is one very long single sentence with no period anywhere in it at all"
    result = extractive_summary(text, max_chars=30)
    assert len(result) <= 30
    # The result must be a prefix built from whole words of the
    # original (plus an ellipsis marker), never a word sliced in half.
    core = result[:-3] if result.endswith("...") else result
    core = core.rstrip()
    assert text.startswith(core)
    assert core == "" or text[len(core):len(core) + 1] in (" ", "")


def test_never_exceeds_max_chars():
    text = "Sentence one is here. " * 50
    result = extractive_summary(text, max_chars=80)
    assert len(result) <= 80


def test_empty_text_returns_empty():
    assert extractive_summary("", max_chars=300) == ""
    assert extractive_summary("   ", max_chars=300) == ""


def test_whitespace_is_collapsed():
    text = "Fixed   the    bug.\n\nTests   pass."
    result = extractive_summary(text, max_chars=300)
    assert "  " not in result
    assert "\n" not in result
