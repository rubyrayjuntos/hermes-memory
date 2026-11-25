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


def test_labeled_password_is_redacted_not_copied_verbatim():
    # 2026-11-24: a real credential leaked into an ask_summary because this
    # function had zero redaction -- a short message fits under max_chars
    # whole and was returned as-is, password included. Fake credential
    # below, never a real one.
    # 2026-11-25: blanket email redaction (added for the Gmail-password
    # leak, which carried its own email address alongside it) now also
    # catches this fixture's plain "username: <email>" -- a deliberate
    # behavior change, not a regression: PII redaction doesn't carve out
    # an exception for "but it was just labeled username".
    text = "username: faketestuser@example.com , pwd: Sup3rFake!Secret"
    result = extractive_summary(text, max_chars=300)
    assert "Sup3rFake!Secret" not in result
    assert "faketestuser@example.com" not in result
    assert "[REDACTED]" in result


def test_labeled_api_key_is_redacted():
    text = "here is the api_key: fAkE1234567890abcdefGHIJKLMNOP for the service"
    result = extractive_summary(text, max_chars=300)
    assert "fAkE1234567890abcdefGHIJKLMNOP" not in result
    assert "[REDACTED]" in result


def test_bearer_token_shape_is_redacted_even_unlabeled():
    text = "Authorization: Bearer fake.jwt.looking.token.abc123XYZ789 sent with the request"
    result = extractive_summary(text, max_chars=300)
    assert "fake.jwt.looking.token.abc123XYZ789" not in result
    assert "[REDACTED]" in result


def test_known_provider_key_prefix_is_redacted_even_unlabeled():
    # sk-/ghp_/AIza-style keys are self-identifying even with no
    # "password:"/"key:" label anywhere near them.
    text = "I pasted sk-fAKEa1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6 into the .env by mistake"
    result = extractive_summary(text, max_chars=300)
    assert "sk-fAKEa1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6" not in result
    assert "[REDACTED]" in result


def test_prose_password_with_no_colon_is_redacted():
    # 2026-11-25: the real leak that exposed a live Gmail password.
    # _LABELED_SECRET_RE only matched "password: X" / "password=X" --
    # a plain sentence ("...with password **X**") has no ':'/'=' at all
    # and sailed straight through unredacted. Fake credential below.
    text = "You gave the persona a Gmail account, with password **Fake9Secret1!**, for sending email."
    result = extractive_summary(text, max_chars=300)
    assert "Fake9Secret1!" not in result
    assert "[REDACTED]" in result
    assert "Gmail account" in result  # redaction, not wholesale suppression


def test_prose_password_markdown_bold_without_double_asterisks_also_redacted():
    text = "the password is Fake9Secret1! and nothing else changed"
    result = extractive_summary(text, max_chars=300)
    assert "Fake9Secret1!" not in result
    assert "[REDACTED]" in result


def test_phone_number_is_redacted():
    # 2026-11-25: a second real leak -- a negative-control answer surfaced
    # an unrelated contact's real phone number unprompted. PII, not just
    # credentials, needs a render-time rule.
    text = "The only phone number I have on file is (832) 468-4304, for your friend."
    result = extractive_summary(text, max_chars=300)
    assert "832" not in result and "4304" not in result
    assert "[REDACTED]" in result
    assert "for your friend" in result


def test_phone_number_dashed_format_is_redacted():
    text = "call 832-468-4304 if you need to reach them"
    result = extractive_summary(text, max_chars=300)
    assert "832-468-4304" not in result
    assert "[REDACTED]" in result


def test_email_address_is_redacted():
    text = "the account is linkiblinki090@gmail.com for outbound mail"
    result = extractive_summary(text, max_chars=300)
    assert "linkiblinki090@gmail.com" not in result
    assert "[REDACTED]" in result
    assert "for outbound mail" in result


def test_prose_password_rule_does_not_fire_on_ordinary_words():
    # 2026-11-25 review: the original _PROSE_PASSWORD_RE matched "pass"
    # followed by ANY next word, so an ordinary sentence using "pass" as
    # a verb/noun (hash collisions, sports, exams...) got redacted as if
    # it were a credential. Real passwords are reliably distinguishable
    # from English words by containing at least one digit -- tightened
    # to require that.
    for text in [
        "we need to fix the pass collisions in the hash table",
        "he will pass the exam next week",
        "the ball was a perfect pass downfield",
    ]:
        result = extractive_summary(text, max_chars=300)
        assert "[REDACTED]" not in result, f"false positive on: {text!r} -> {result!r}"


def test_jwt_shape_rule_does_not_fire_on_domains_ips_or_versions():
    # 2026-11-25 review: the original _JWT_SHAPE_RE matched ANY
    # dot-separated triple of word characters -- domains
    # (www.linkedin.com), IPs (127.0.0.1), and version strings
    # (3.13.11.final.0) all sailed in as false "JWTs". Real JWTs are
    # near-universally identifiable by their header segment, which is
    # the base64 encoding of a JSON object starting with '{"' --
    # that encoding always starts with "eyJ". Tightened to require it.
    for text in [
        "check www.linkedin.com for the profile",
        "the server is at 127.0.0.1 on the local box",
        "running Python 3.13.11.final.0 in the venv",
        "see docs.python.org for the reference",
    ]:
        result = extractive_summary(text, max_chars=300)
        assert "[REDACTED]" not in result, f"false positive on: {text!r} -> {result!r}"
        assert text in result  # untouched, not even partially mangled


def test_jwt_shape_rule_still_fires_on_a_real_looking_jwt():
    text = (
        "Authorization header was eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        " sent with the request"
    )
    result = extractive_summary(text, max_chars=300)
    assert "eyJhbGciOiJIUzI1NiJ9" not in result
    assert "[REDACTED]" in result


def test_redaction_runs_before_the_character_budget_not_after():
    # Redact first, then apply the existing max_chars/sentence-boundary
    # logic on the already-safe text -- never truncate first and leave a
    # half-redacted secret in the kept prefix.
    text = "pwd: Sup3rFake!Secret12345678901234567890. Then I ran the tests and they passed."
    result = extractive_summary(text, max_chars=20)
    assert "Sup3rFake!Secret12345678901234567890" not in result
    assert len(result) <= 20
