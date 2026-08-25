"""P7 — file-hash + state roundtrip; corrupt JSON yields {}.

Also covers chunk_text losslessness (the P-surface C4 flagged).
"""
from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st


from strategies import any_text  # noqa: E402

from hermes_memory.ingest import (  # noqa: E402
    MAX_CHUNK_CHARS,
    chunk_text,
    file_hash,
    load_state,
    save_state,
)


@settings(max_examples=100)
@given(st.data(), any_text)
def test_p7_file_hash_stable(data, content):
    tmp = data.draw(st.just(None))
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(content)
        p = Path(fh.name)
    try:
        h1 = file_hash(p)
        h2 = file_hash(p)
        assert h1 == h2 and len(h1) == 64
        # any byte change must change the hash
        p.write_text(content + "x")
        assert file_hash(p) != h1 or content == ""
    finally:
        p.unlink(missing_ok=True)


@settings(max_examples=200)
@given(st.dictionaries(
    st.text(alphabet="abc./_-", min_size=1, max_size=20),
    st.text(min_size=0, max_size=64).filter(lambda s: all(c.isalnum() for c in s)),
    max_size=10,
))
def test_p7_state_roundtrip(state):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "state.json"
        save_state(p, state)
        assert load_state(p) == state


def test_p7_corrupt_json_yields_empty():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        p.write_text("{not json at all")
        assert load_state(p) == {}
        p.write_bytes(b"\xff\xfe\x00binary garbage")
        assert load_state(p) == {}
        assert load_state(Path(d) / "missing.json") == {}


# -- chunk losslessness -------------------------------------------------------

@settings(max_examples=300)
@given(any_text)
def test_chunk_lossless(text):
    chunks = chunk_text(text)
    assert "".join(chunks) == text
    if text:
        assert all(chunks)
    for c in chunks[:-1]:
        assert len(c) <= max(MAX_CHUNK_CHARS, len(text))
