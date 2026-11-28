from hermes_memory.extract_nouns import extract_nouns


def test_pascal_and_aliases():
    labels = [n.label for n in extract_nouns(
        "RateLimiter talks to nomic-embed-text via hermes agent"
    )]
    assert labels == ["RateLimiter", "nomic-embed-text", "Hermes Agent"]


def test_first_mint_lowercase_multiword():
    labels = [n.label for n in extract_nouns(
        "the rate limiter talks to nomic-embed-text"
    )]
    assert "RateLimiter" not in labels
    assert "rate limiter" in labels
    assert "nomic-embed-text" in labels


def test_noise_empty():
    assert extract_nouns("Got it. Perfect.") == []


def test_quota_five():
    spans = " ".join(f"`token{i}`" for i in range(12))
    out = extract_nouns(spans)
    assert len(out) <= 5
    assert [n.mention_index for n in out] == sorted(n.mention_index for n in out)


def test_denylist():
    labels = {n.label.lower() for n in extract_nouns("see `id` and `conf` and RateLimiter")}
    assert "id" not in labels
    assert "conf" not in labels
    assert "RateLimiter" in {n.label for n in extract_nouns("see RateLimiter")}


def test_go_without_cue_dropped():
    assert all("go" != n.label.lower() for n in extract_nouns("please go ahead"))


def test_synthetics_on_synthetic_session():
    short = "Project Zephyr and Atlas Vault Engine for verification"
    assert extract_nouns(short, synthetic_session=True) == []

    fixture = (
        "C5 verify synthetic user turn: please remember that Project Zephyr uses "
        "the Atlas Vault Engine for its storage layer."
    )
    assert len(fixture) >= 40
    labels = {n.label.lower() for n in extract_nouns(fixture, synthetic_session=True)}
    for banned in (
        "project zephyr",
        "atlas vault engine",
        "atlas vault",
        "vault engine",
        "zephyr",
    ):
        assert banned not in labels


def test_synthetics_non_session_mints_title_case():
    text = (
        "We deployed Atlas Vault Engine appear in production today "
        "for the release checkpoint."
    )
    labels = [n.label for n in extract_nouns(text, synthetic_session=False)]
    assert "Atlas Vault Engine" in labels
    assert not any(l.lower().endswith("appear") for l in labels)


def test_issue79_rejects_contraction_and_aux_fragments():
    from hermes_memory.extract_nouns import is_junk_fragment_label

    text = (
        "i am thinking about this in terms of tokyo eye protein topology "
        "and this isn't necessary and don't do that"
    )
    labels = [n.label for n in extract_nouns(text)]
    low = {n.lower() for n in labels}
    for banned in ("am thinking", "i am", "i am thinking", "isn t", "don t"):
        assert banned not in low
    assert "Tokyo Eye" in labels
    assert is_junk_fragment_label("isn t")
    assert is_junk_fragment_label("don t")
    assert is_junk_fragment_label("ll keep")
    assert not is_junk_fragment_label("Tokyo Eye")
    assert not is_junk_fragment_label("as-is")
    assert not is_junk_fragment_label("M src/hermes_memory/graph_api.py")
    assert not is_junk_fragment_label("Model T")
    assert not is_junk_fragment_label("Vitamin D")
    assert not is_junk_fragment_label("tin can")
    labels_can = [n.label.lower() for n in extract_nouns("the tin can talks to RateLimiter")]
    assert "tin can" in labels_can
    assert not is_junk_fragment_label("rate limiter is")


# -- issue #123: candidate_label_slugs() lets a caller fetch only the noun
# rows that could possibly resolve this turn's text, instead of the whole
# `noun` table (see Store.fetch_noun_labels_for_slugs / provider.py). These
# tests prove it stays behaviorally identical to the old full-table lookup.


def test_candidate_label_slugs_matches_slug_of_every_resolvable_surface():
    from hermes_memory.extract_nouns import _slug, candidate_label_slugs

    text = "RateLimiter talks to nomic-embed-text via `token1` and the rate limiter"
    slugs = candidate_label_slugs(text)
    # Every slug the real resolution path would consult (_resolve_label calls
    # _slug(surface) for each surviving candidate) must be present.
    assert _slug("RateLimiter") in slugs
    assert _slug("nomic-embed-text") in slugs
    assert _slug("token1") in slugs
    # No duplicates -- it's a lookup key set, not a multiset.
    assert len(slugs) == len(set(slugs))


def test_candidate_label_slugs_empty_for_noise_text():
    from hermes_memory.extract_nouns import candidate_label_slugs

    assert candidate_label_slugs("") == []
    assert candidate_label_slugs("Got it. Perfect.") == []


def test_scoped_existing_labels_give_identical_results_to_full_table():
    """The whole point of #123: filtering existing_labels down to only the
    rows whose slug is in candidate_label_slugs(text) must never change what
    extract_nouns() returns, compared to passing every label in the table."""
    from hermes_memory.extract_nouns import candidate_label_slugs, _slug

    # A simulated "whole table" -- including labels with a different surface
    # but same canonical slug as a candidate (this is exactly the case the
    # old full fetch existed to catch), plus many rows totally unrelated to
    # the turn, which the real fix must not need to read at all.
    whole_table = [
        "RateLimiter", "Rate-Limiter", "nomic embed text", "Hermes Agent",
        "Completely Unrelated Thing", "Another Unrelated Noun", "Zephyr",
    ]
    texts = [
        "RateLimiter talks to nomic-embed-text via hermes agent",
        "the rate limiter talks to nomic-embed-text",
        "Got it. Perfect.",
        "`token1` `token2` `token3`",
    ]
    for text in texts:
        candidate_slugs = set(candidate_label_slugs(text))
        scoped_labels = [lbl for lbl in whole_table if _slug(lbl) in candidate_slugs]

        full_result = extract_nouns(text, existing_labels=whole_table)
        scoped_result = extract_nouns(text, existing_labels=scoped_labels)
        assert full_result == scoped_result, (
            f"scoping existing_labels changed the result for {text!r}: "
            f"full={full_result!r} scoped={scoped_result!r}"
        )
