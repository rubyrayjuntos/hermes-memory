"""CI-gated ANN golden set: (query vector → expected turn_id), not one injection example."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_memory.store import clamp_hnsw_ef_search

GOLDEN_PATH = Path(__file__).parent / "data" / "ann_golden_turn_ids.json"


def _load_cases() -> list[dict]:
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return list(payload["cases"])


def _one_hot(axis: int, dim: int = 768) -> str:
    vals = ["0.0"] * dim
    vals[int(axis)] = "1.0"
    return "[" + ",".join(vals) + "]"


def _near(axis: int, dim: int = 768) -> str:
    """Same pole as `axis` with a little mass on the next axis — still nearest to that pole."""
    vals = ["0.0"] * dim
    vals[int(axis)] = "0.95"
    vals[(int(axis) + 1) % dim] = "0.05"
    return "[" + ",".join(vals) + "]"


def test_ann_golden_set_has_multiple_frozen_cases() -> None:
    cases = _load_cases()
    assert len(cases) >= 4
    axes = [int(c["axis"]) for c in cases]
    assert len(set(axes)) == len(axes)
    assert all(c.get("content") and c.get("id") for c in cases)


def test_fountain_debug_pane_shows_ef_search() -> None:
    html = (Path(__file__).resolve().parents[1] / "docs" / "graph" / "fountain.html").read_text(
        encoding="utf-8"
    )
    assert "hnsw.ef_search=" in html
    assert "ef_search=${ef}" in html


@pytest.mark.store
@pytest.mark.integration
@pytest.mark.asyncio
async def test_ann_golden_turn_ids_rank_first(store, clean_hermes_test_db, hermes_test_dsn, caplog):
    assert "hermes_test" in hermes_test_dsn
    assert "hermes_memory" not in hermes_test_dsn or "hermes_test" in hermes_test_dsn

    cases = _load_cases()
    ids: dict[str, int] = {}
    for case in cases:
        turn_id = await store.insert_turn(
            case["session_id"],
            "default",
            "user",
            case["content"],
            _one_hot(case["axis"]),
            {"kind": "interactive", "golden": case["id"]},
        )
        assert turn_id is not None
        ids[case["id"]] = int(turn_id)

    import logging

    with caplog.at_level(logging.INFO, logger="hybrid_age.store"):
        for case in cases:
            rows = await store.vector_search(_one_hot(case["axis"]), k=5)
            conv = [r for r in rows if r.get("src") == "conversation"]
            assert conv, f"no conversation hits for {case['id']}"
            top = conv[0]
            assert int(top["id"]) == ids[case["id"]], (
                f"{case['id']}: expected turn {ids[case['id']]}, got {top['id']} "
                f"(sim={top.get('similarity')})"
            )
            distractors = [c for c in cases if c["id"] != case["id"]]
            distractor_ids = {ids[d["id"]] for d in distractors}
            assert int(top["id"]) not in distractor_ids

        noisy = await store.vector_search(_near(0), k=5)
        noisy_conv = [r for r in noisy if r.get("src") == "conversation"]
        assert noisy_conv
        assert int(noisy_conv[0]["id"]) == ids["ratelimiter_axis0"]

    assert "hnsw.ef_search=" in caplog.text
    assert str(clamp_hnsw_ef_search(store.hnsw_ef_search)) in caplog.text
