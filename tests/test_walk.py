from __future__ import annotations

import math

from hermes_memory.walk import (
    beam_score,
    clamp_cos,
    consensus_decay,
    provenance_boost,
)


def test_clamp_negative_pole() -> None:
    assert clamp_cos(-0.4) == 0.0
    assert clamp_cos(0.5) == 0.5


def test_provenance_boost_is_binary() -> None:
    assert provenance_boost(10, [10, 11]) == 1.0
    assert provenance_boost(9, [10, 11]) == 0.0


def test_empty_incident_decay_is_one() -> None:
    assert (
        consensus_decay(
            empty_incident=True,
            local_dir=0.0,
            query_dir=0.0,
            hop=1,
        )
        == 1.0
    )


def test_nonempty_consensus_decay_uses_hop() -> None:
    decay = consensus_decay(
        empty_incident=False,
        local_dir=0.5,
        query_dir=0.4,
        hop=2,
    )
    assert decay == math.exp(-0.05 * 2 * (1.0 - 0.5 * 0.4))


def test_score_scales_by_magnitude_over_eight() -> None:
    c, composite, score = beam_score(
        sim=1.0,
        src_align=1.0,
        tgt_align=1.0,
        prov_boost=1.0,
        decay=1.0,
        magnitude=8.0,
    )
    assert c == 1.0
    assert abs(score - composite) < 1e-9
    _, _, half = beam_score(
        sim=1.0,
        src_align=1.0,
        tgt_align=1.0,
        prov_boost=1.0,
        decay=1.0,
        magnitude=4.0,
    )
    assert abs(half - 0.5 * score) < 1e-9


def test_score_formula_uses_clamped_pole_product() -> None:
    c, composite, score = beam_score(
        sim=0.75,
        src_align=clamp_cos(-0.2),
        tgt_align=clamp_cos(0.8),
        prov_boost=1.0,
        decay=0.9,
        magnitude=4.0,
    )
    assert c == 0.0
    assert composite == 0.4 * 0.75 + 0.2 * 0.9
    assert score == composite * (4.0 / 8.0)
