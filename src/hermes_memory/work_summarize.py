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

# 2026-11-24: a real credential leaked into an ask_summary verbatim --
# this module did purely extractive summarization (correctly avoiding
# generative paraphrase) but had no redaction step at all, so any short
# message fit under max_chars whole and came out with secrets intact.
# Two tiers, applied before anything else touches the text:
#   1. Labeled key: value pairs for common secret-ish labels (password,
#      pwd, pass, secret, token, api key, apikey, auth) -- redact the
#      value, keep the rest of the message (this is redaction, not
#      wholesale suppression of an otherwise-ordinary message).
#   2. Self-identifying shapes that don't need a label at all: Bearer
#      tokens, JWT-looking dotted triples, and known provider key
#      prefixes (sk-, ghp_, gho_, AIza, xox[abp]-).
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(password|pwd|pass|secret|token|api[_ -]?key|apikey|auth)\b"
    r"\s*[:=]\s*(\S+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+(\S+)")
# 2026-11-25 review: tightened to require the "eyJ" marker (the base64
# encoding of a JSON header's opening '{"') -- without it this matched
# any dot-separated triple of word chars at all: domains
# (www.linkedin.com), IPs (127.0.0.1), version strings (3.13.11.final.0).
_JWT_SHAPE_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PROVIDER_KEY_PREFIX_RE = re.compile(
    r"\b(sk-|ghp_|gho_|ghu_|ghs_|AIza|xox[abp]-)[A-Za-z0-9_-]{10,}\b"
)


# 2026-11-25: two more real leaks, both through paths this module
# didn't cover at all:
#   3. A password stated in PROSE with no ':'/'=' at all ("...with
#      password **X**...") -- _LABELED_SECRET_RE requires a separator
#      and never fires on plain sentences. Rule: the cue word itself
#      ("password"/"pwd"/"pass", optionally preceded by "the"/"is"/
#      markdown bold) followed directly by the next token, redact that
#      token regardless of separator.
#   4. PII that isn't a credential at all -- phone numbers and email
#      addresses. A negative-control answer surfaced an unrelated real
#      contact's phone number unprompted; the Gmail password leak also
#      carried the account's own email address alongside it.
_PROSE_PASSWORD_RE = re.compile(
    r"(?i)\b(password|pwd|pass)\b\s+(?:is\s+)?\**([^\s*,.;:]*\d[^\s*,.;:]*)\**"
)
_PHONE_RE = re.compile(
    r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)


def _redact_secrets(text: str) -> str:
    """Mask credential-shaped substrings; leave the rest of the message
    intact. Order matters: labeled pairs first (so a labeled token's own
    value doesn't also get caught, harmlessly, by a shape rule below),
    then the self-identifying shapes that need no label."""
    redacted = _LABELED_SECRET_RE.sub(
        lambda m: f"{m.group(1)}: [REDACTED]", text
    )
    redacted = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _PROVIDER_KEY_PREFIX_RE.sub("[REDACTED]", redacted)
    redacted = _JWT_SHAPE_RE.sub("[REDACTED]", redacted)
    redacted = _PROSE_PASSWORD_RE.sub(
        lambda m: f"{m.group(1)}: [REDACTED]", redacted
    )
    redacted = _PHONE_RE.sub("[REDACTED]", redacted)
    redacted = _EMAIL_RE.sub("[REDACTED]", redacted)
    return redacted


def extractive_summary(text: str, max_chars: int) -> str:
    """Whole leading sentences, collapsed whitespace, up to max_chars.
    Never cuts mid-sentence when more than one sentence fits; never
    cuts mid-word even when a single sentence must be truncated.
    Secrets are redacted before anything else happens to the text (see
    _redact_secrets) -- truncation never gets a chance to leave a
    half-redacted credential in the kept prefix."""
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if not normalized:
        return ""
    normalized = _redact_secrets(normalized)
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
