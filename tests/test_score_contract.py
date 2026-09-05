"""Retrieval scoring contract: one live formula, no silent substitutes.

Graph relevance is ``beam_score`` only. ANN ``1 - (embedding <=> q)`` is
candidate generation, not a second weighted sum. Missing walker slot 6 is
None — never recomputed. Catalog and bench must call ``beam_score``, not
invent coefficients.
"""
from __future__ import annotations

import ast
from pathlib import Path

from hermes_memory.graph_api import unpack_expand_row
from hermes_memory.provider_helpers import internal_rank
from hermes_memory.walk import beam_score

_SRC = Path(__file__).resolve().parents[1] / "src" / "hermes_memory"


def test_beam_score_is_the_only_weighted_sum() -> None:
    c, composite, score = beam_score(
        sim=0.9,
        src_align=1.0,
        tgt_align=1.0,
        prov_boost=1.0,
        decay=1.0,
        magnitude=8.0,
    )
    assert c == 1.0
    assert abs(composite - (0.4 * 0.9 + 0.4 * 1.0 + 0.2 * 1.0)) < 1e-9
    assert abs(score - composite) < 1e-9


def test_beam_score_increases_with_decay() -> None:
    common = dict(sim=0.5, src_align=1.0, tgt_align=1.0, prov_boost=1.0, magnitude=8.0)
    _, _, lo = beam_score(decay=0.2, **common)
    _, _, hi = beam_score(decay=0.9, **common)
    assert hi > lo


def test_unpack_7tuple_keeps_slot_6() -> None:
    walker_score = 0.37
    _, _, _, _, _, decay, score, hop = unpack_expand_row(
        ("n", "mentions", "m", 5.8, 0.18, 0.97, walker_score)
    )
    assert hop == 1
    assert decay == 0.97
    assert score == walker_score


def test_unpack_none_score_stays_none() -> None:
    _, _, _, _, _, _, score, _ = unpack_expand_row(
        ("n", "mentions", "m", 1.0, 0.72, 0.4, None)
    )
    assert score is None


def test_unpack_short_tuple_has_no_score() -> None:
    _, _, _, _, _, decay, score, hop = unpack_expand_row(
        ("n", "mentions", "m", 1.0, 0.72, 2)
    )
    assert hop == 2
    assert decay is None
    assert score is None


def test_internal_rank_prefers_walker_best_score() -> None:
    assert internal_rank({
        "best_score": 0.37,
        "best_weight": 5.8,
        "best_cosine": 0.18,
        "best_decay": 0.97,
        "score": 0.8,
    }) == 0.37


def test_internal_rank_falls_back_to_ann_similarity_not_legacy() -> None:
    got = internal_rank({
        "best_weight": 1.0,
        "best_cosine": 0.72,
        "best_decay": 0.4,
        "score": 0.8,
    })
    assert got == 0.8
    assert abs(got - (0.5 * 0.72 + 0.3 * 1.0 + 0.2 * 0.4)) > 0.05


def test_legacy_names_are_gone() -> None:
    for name in ("walk.py", "store.py", "provider.py", "graph_api.py", "provider_helpers.py"):
        text = (_SRC / name).read_text(encoding="utf-8")
        assert "legacy_composite" not in text, name
        assert "recency_score" not in text, name


def _numeric_weighted_sums(tree: ast.AST) -> list[ast.AST]:
    """BinOp trees that add 3+ numeric-coefficient products (a*x + b*y + c*z)."""

    def is_num(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, (int, float))

    def coeff_product(node: ast.AST) -> bool:
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
            return False
        return is_num(node.left) or is_num(node.right)

    def flatten_add(node: ast.AST) -> list[ast.AST]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return flatten_add(node.left) + flatten_add(node.right)
        return [node]

    found: list[ast.AST] = []

    class V(ast.NodeVisitor):
        def visit_BinOp(self, node: ast.BinOp) -> None:
            self.generic_visit(node)
            if not isinstance(node.op, ast.Add):
                return
            terms = flatten_add(node)
            if sum(1 for t in terms if coeff_product(t)) >= 3:
                found.append(node)

    V().visit(tree)
    return found


def test_only_beam_score_may_define_a_three_term_weighted_sum() -> None:
    """Catches 0.51*sim + 0.29*w + 0.2*decay, not just the old 0.5/0.3 literals."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                path.name == "walk.py"
                and isinstance(node, ast.FunctionDef)
                and node.name == "beam_score"
            ):
                continue
            for hit in _numeric_weighted_sums(node):
                offenders.append(f"{path.name}:{hit.lineno}")
    assert offenders == [], "extra weighted-sum score:\n" + "\n".join(offenders)
