"""Spec 4 T4.3: turn_filter.strip_memory_blocks removes injected context
blocks from extractor input before anything else touches the text."""
from hermes_memory.turn_filter import strip_memory_blocks


def test_strips_memory_block_entirely():
    text = (
        "Here is my answer.\n"
        "<memory>\n  <grounded>\n    some injected fact\n  </grounded>\n</memory>\n"
        "And some more prose after."
    )
    out = strip_memory_blocks(text)
    assert "<memory>" not in out
    assert "injected fact" not in out
    assert "Here is my answer." in out
    assert "And some more prose after." in out


def test_strips_past_context_block_entirely():
    text = "Before.\n<PAST_CONTEXT>\nold session notes\n</PAST_CONTEXT>\nAfter."
    out = strip_memory_blocks(text)
    assert "<PAST_CONTEXT>" not in out
    assert "old session notes" not in out
    assert "Before." in out
    assert "After." in out


def test_strips_grounded_facts_block_with_attributes():
    text = (
        'Reply text.\n<grounded_facts spans="1,2,3">\n  <fact id="c1">lives_in NYC</fact>\n'
        "</grounded_facts>\nTrailing."
    )
    out = strip_memory_blocks(text)
    assert "<grounded_facts" not in out
    assert "lives_in NYC" not in out
    assert "Reply text." in out
    assert "Trailing." in out


def test_strips_supporting_turns_block():
    text = "Intro.\n<supporting_turns>\n  turn 1 text\n</supporting_turns>\nOutro."
    out = strip_memory_blocks(text)
    assert "<supporting_turns>" not in out
    assert "turn 1 text" not in out
    assert "Intro." in out
    assert "Outro." in out


def test_preserves_code_fence_that_looks_like_a_tag():
    """A pasted snippet showing the literal tag syntax must survive --
    it is documentation/example text, not an actual injected block."""
    text = (
        "Here's how the injection looks:\n"
        "```text\n<memory><grounded>fake</grounded></memory>\n```\n"
        "End of example."
    )
    out = strip_memory_blocks(text)
    assert "<memory><grounded>fake</grounded></memory>" in out
    assert "End of example." in out


def test_unmatched_opening_tag_removes_through_end_of_input():
    text = "Prose before.\n<memory>\nnever closed, trails off"
    out = strip_memory_blocks(text)
    assert "Prose before." in out
    assert "never closed" not in out


def test_unmatched_closing_tag_removed_without_discarding_prose():
    text = "Some prose.\n</memory>\nMore prose after the stray close tag."
    out = strip_memory_blocks(text)
    assert "</memory>" not in out
    assert "Some prose." in out
    assert "More prose after the stray close tag." in out


def test_mixed_reply_keeps_remaining_authored_text():
    text = (
        "I'll remember that you live in Austin.\n"
        "<memory><grounded>unrelated injected fact</grounded></memory>\n"
        "Anything else?"
    )
    out = strip_memory_blocks(text)
    assert "I'll remember that you live in Austin." in out
    assert "Anything else?" in out
    assert "unrelated injected fact" not in out
