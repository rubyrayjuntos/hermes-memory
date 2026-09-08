"""semantic_edge dim mismatch must not silently skip UPDATE (#70 / D-MVP-6)."""
from __future__ import annotations

import inspect

from hermes_memory.store import Store


def test_upsert_mentions_chain_raises_on_dim_mismatch() -> None:
    src = inspect.getsource(Store.upsert_mentions_chain)
    assert "semantic_edge dim mismatch" in src
    assert "len(old_tgt) != len(turn_vec)" in src
    after = src.split("len(old_tgt) != len(turn_vec)", 1)[1][:240]
    assert "raise" in after
    assert "continue" not in after.split("raise", 1)[0]
