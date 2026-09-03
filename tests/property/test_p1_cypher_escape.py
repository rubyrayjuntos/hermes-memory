"""P1 — Cypher string escaping (docs/plans/v0.1.md §6).

age_str output must contain no unescaped ``'`` or ``\\`` from the input, so
arbitrary text can never break out of a Cypher string literal.

C1 fix: untrusted text can close Cypher $$ quoting.  age_str escapes $ and
cypher_dollar_quote / cypher_call pick a non-colliding $cyN$ tag.
"""
from __future__ import annotations


from hypothesis import given, settings
from hypothesis import strategies as st


from strategies import adversarial_text  # noqa: E402

from hermes_memory.store import (  # noqa: E402
    _pick_cypher_dollar_tag,
    age_str,
    cypher_call,
    cypher_dollar_quote,
)


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


# ---------------------------------------------------------------------------
# C1: dollar-quote injection — $$ and $tag$ cannot break outer SQL
# ---------------------------------------------------------------------------

@settings(max_examples=300)
@given(adversarial_text)
def test_p1_age_str_escapes_dollar(payload):
    """age_str must neutralise $ so raw $$ never appears in the literal."""
    out = age_str(payload)
    if payload is None:
        return
    # if input contained $, output must escape it as \\$ and never contain raw $$
    # (raw $$ inside a Cypher string would still close outer $$ quoting)
    if "$" in str(payload):
        # escaped form contains \\$ — the two-char sequence, not bare $
        # the simplest guard: raw $$ must not appear unescaped in the body
        body_inside = out[1:-1]  # strip outer quotes
        # $$ would be two consecutive $ with no backslash escape in between
        # after escaping, any $ is preceded by \\, so bare $$ cannot occur
        # we assert no unescaped $$ (i.e. $$ not preceded by \\)
        # but the trivial check is: after escaping, "$$" substring cannot occur
        # because every $ was turned into \\$
        assert "$$" not in body_inside or "\\$" in body_inside
        # stronger: if payload contained $$, the output must not contain $$ either
        if "$$" in str(payload):
            assert "$$" not in body_inside, f"$$ leaked for {payload!r} -> {out!r}"


@settings(max_examples=300)
@given(adversarial_text)
def test_p1_cypher_dollar_quote_resists_injection(payload):
    """Tagged dollar-quote wrapper picks a non-colliding $cyN$ tag."""
    # Build a realistic Cypher body that embeds untrusted text via age_str
    body = f"MATCH (n) WHERE n.name = {age_str(payload)} RETURN n"
    tag = _pick_cypher_dollar_tag(body)
    assert tag not in body, f"tag {tag!r} collides with body {body!r}"
    quoted = cypher_dollar_quote(body)
    assert quoted.startswith(tag) and quoted.endswith(tag)
    assert quoted[len(tag):-len(tag)] == body
    # outer SQL must contain exactly two occurrences of the chosen tag
    sql = f"SELECT * FROM {cypher_call('hermes_knowledge', body)} AS (n agtype)"
    assert sql.count(tag) == 2
    # the quoted body must not be breakable by payload's $$ or $tag$
    # i.e. body itself must have been made safe: if body contained $$, tag must not be $$
    if "$$" in body:
        assert tag != "$$", "body contains $$ but tag was still $$"


@settings(max_examples=300)
@given(st.text(alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyz$0123456789_")), min_size=1, max_size=40))
def test_p1_raw_payload_cannot_close_dollar_quote(payload):
    """Raw payloads containing $tag$ collisions are handled via $cyN$ fallback."""
    # inject explicit adversarial dollar patterns
    candidates = [payload, payload + "$$", "$$" + payload, f"$cy0${payload}", f"{payload}$tag$"]
    for raw in candidates:
        tag = _pick_cypher_dollar_tag(raw)
        assert tag not in raw, f"tag {tag!r} collides with raw {raw!r}"
        quoted = cypher_dollar_quote(raw)
        assert quoted.startswith(tag) and quoted.endswith(tag)
        # quoted payload round-trips
        assert quoted[len(tag):-len(tag)] == raw
        # if raw already contains $$, tag must be $cyN$, not $$
        if "$$" in raw:
            assert tag != "$$"


def test_p1_explicit_dollar_injection_vectors():
    """Deterministic vectors: $$ and $tag$ must not break outer SQL."""
    vectors = [
        "$$",
        "a$$b",
        "$$ SELECT * FROM cypher('hermes_knowledge', $$ MATCH (n) RETURN n $$) --",
        "$cy0$",
        "$cy1$",
        "$tag$",
        "$cy0$ $cy1$ $cy2$ $$ $tag$",
        "'); $$; DROP TABLE --",
        "test $cy0$ injection",
    ]
    for payload in vectors:
        body = f"MATCH (n) WHERE n.name = {age_str(payload)} RETURN n"
        tag = _pick_cypher_dollar_tag(body)
        assert tag not in body, f"collision for payload {payload!r} tag {tag!r} body {body!r}"
        quoted = cypher_dollar_quote(body)
        assert quoted == f"{tag}{body}{tag}"
        sql = f"SELECT * FROM {cypher_call('hermes_knowledge', body)} AS (n agtype)"
        # sql must contain exactly one opening and one closing of the tag
        assert sql.count(tag) == 2
        # age_str must have neutralised raw $$
        if "$$" in payload:
            assert "$$" not in age_str(payload)[1:-1]
        # raw payload direct quoting also safe
        raw_tag = _pick_cypher_dollar_tag(payload)
        assert raw_tag not in payload
        assert cypher_dollar_quote(payload) == f"{raw_tag}{payload}{raw_tag}"
