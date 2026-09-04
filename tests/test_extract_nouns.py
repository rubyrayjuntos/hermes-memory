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
