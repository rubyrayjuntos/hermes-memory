"""Test should_enqueue_model does not enqueue content-free / non-authored text.

T3.6 follow-up bug: when rules extract zero claims AND the text has no
first-person cue, should_enqueue_model was falling through to
`return not self_claims`, which is True whenever self_claims is empty --
i.e. almost always. This sent meta-instructions and pasted-doc preambles
(with the quoted block stripped out) to the model, diluting the batch
sent for turns that actually needed model extraction.
"""
from hermes_memory.extraction_jobs import should_enqueue_model


def test_pasted_doc_preamble_not_enqueued():
    """'Please summarize this pasted resume:' (code block already segmented
    out) has no first-person cue and no claims -- must not enqueue."""
    text = "Please summarize this pasted resume:\n\n```text\nI live in Boston and work at Initech.\n```"
    assert should_enqueue_model(text) is False


def test_plain_instruction_not_enqueued():
    """A bare instruction/question-less imperative with no self-content."""
    assert should_enqueue_model("Summarize the attached document.") is False
    assert should_enqueue_model("Please proofread this.") is False


def test_implicit_verb_turn_still_enqueued():
    """Regression guard: real implicit-first-person paraphrases still enqueue."""
    assert should_enqueue_model("moved back home to New York last spring") is True
    assert should_enqueue_model("Started at Initech in March") is True
    assert should_enqueue_model("these days I'm doing staff engineering") is True
