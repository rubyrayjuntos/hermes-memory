"""P2 — age_props: None dropped, keys preserved, unsafe keys rejected."""
from __future__ import annotations

import re

from hypothesis import given, settings


from strategies import (  # noqa: E402
    dicts_with_unsafe_keys,
    property_maps,
    safe_identifier,
)

from hermes_memory.store import _SAFE_IDENT, age_props, age_str  # noqa: E402


@settings(max_examples=300)
@given(property_maps)
def test_p2_none_dropped_and_keys_preserved(props):
    out = age_props(props)
    body = out[1:-1]
    # Each valid, non-None key renders its full pair exactly once.
    for key, val in props.items():
        if val is None or not _SAFE_IDENT.match(key or ""):
            continue
        pair = f"{key}: {age_str(val)}"
        assert re.search(rf"(?<![A-Za-z0-9_]){re.escape(pair)}", body), \
            f"expected exactly one {pair!r} in {out!r}"
    # No other key/value pairs were invented: count rendered pairs by
    # splitting on top-level ", " (values are quoted strings, so a value can
    # contain ", " — match against the expected rendered pairs instead).
    n_expected = sum(
        1 for k, v in props.items()
        if v is not None and _SAFE_IDENT.match(k or "")
    )
    remaining = body
    for k, v in props.items():
        if v is not None and _SAFE_IDENT.match(k or ""):
            pair = f"{k}: {age_str(v)}"
            remaining = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(pair)}", "", remaining,
                               count=1)
    assert remaining.strip(", ") == "" and (
        body == "" if n_expected == 0 else True
    )


@settings(max_examples=200)
@given(dicts_with_unsafe_keys())
def test_p2_unsafe_keys_rejected(props):
    """Injection-shaped keys must never appear verbatim in the output."""
    out = age_props(props)
    body = out[1:-1]
    for key in props:
        if key and not _SAFE_IDENT.match(key):
            assert f"{key}: " not in body, f"unsafe key leaked: {key!r}"


@settings(max_examples=100)
@given(safe_identifier)
def test_p2_check_label_roundtrip(label):
    from hermes_memory.store import check_label

    assert check_label(label) == label
