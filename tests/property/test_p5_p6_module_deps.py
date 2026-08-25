"""P5 — module-name heuristic rejects artifacts; P6 — dependency extraction.

Also exercises the nearest P3 surrogate: the provider topic window never grows
unbounded and stores only capitalized phrases.
"""
from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from strategies import any_text, python_import_names  # noqa: E402

from hermes_memory.ingest import (  # noqa: E402
    _is_module_name,
    dep_kind,
    extract_dependencies,
    module_for,
)
from hermes_memory.provider import HybridAgeMemoryProvider  # noqa: E402

# Artifact names drawn from the same classes the production heuristic targets.
module_artifacts = st.sampled_from([
    "deadbeef", "DEADBEEF01", "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
    "550e8400-e29b-41d4-a716-446655440000",
    "550E8400E29B41D4A716446655440000",
    "123456", "17092345678901",
    "src", "lib", "app", "__pycache__", "node_modules", "dist", "build",
    "target", ".git", ".venv", "venv", "site-packages", "cache", "tmp",
    "artifacts", "mlruns", "checkpoints", "logs", "output", "tests",
    "fixtures", "..", ".", "",
])

real_module_names = st.from_regex(
    r"[a-zA-Z][a-zA-Z0-9_-]{1,29}", fullmatch=True
).filter(_is_module_name)


# -- P5 -----------------------------------------------------------------------

@settings(max_examples=200)
@given(module_artifacts)
def test_p5_rejects_artifacts(name):
    assert not _is_module_name(name), f"artifact accepted as module: {name!r}"


@settings(max_examples=200)
@given(real_module_names)
def test_p5_accepts_real_names(name):
    assert _is_module_name(name), f"real name rejected: {name!r}"


@settings(max_examples=100)
@given(real_module_names)
def test_p5_module_finds_deepest_real_segment(mod):
    assert module_for(f"src/{mod}/service.ts") == mod
    assert module_for(f"{mod}/main.py") == mod


# -- P6 -----------------------------------------------------------------------

def _py_source(deps):
    return "\n".join(f"import {d}" for d in deps), {d.split(".")[0] for d in deps}


@settings(max_examples=300)
@given(st.lists(python_import_names, min_size=0, max_size=6))
def test_p6_deterministic_sorted_unique(deps):
    source, expected = _py_source(deps)
    out1 = extract_dependencies(source, "Python")
    out2 = extract_dependencies(source, "Python")
    assert out1 == out2                        # deterministic
    assert out1 == sorted(out1)                # sorted
    assert len(out1) == len(set(out1))         # unique
    assert expected <= set(out1)


def test_p6_dep_kind():
    assert dep_kind("./utils") == "internal"
    assert dep_kind("/abs/path") == "internal"
    assert dep_kind("@/components/Button") == "internal"
    assert dep_kind("react") == "external"


@settings(max_examples=100)
@given(any_text)
def test_p6_never_raises_on_any_text(text):
    for lang in ("Python", "TypeScript", "TypeScriptReact", "Documentation"):
        out = extract_dependencies(text, lang)
        assert out == sorted(set(out))


# -- P3 surrogate -------------------------------------------------------------

def test_topic_window_guards():
    """_extract_topics keeps only short capitalized phrases; window bounded."""
    p = object.__new__(HybridAgeMemoryProvider)
    p._recent_topics = []
    p._max_topics = 8
    text = ("Ok then Atlas Vault Engine met Project X\nnewline Should Not Span "
            + "x" * 500)
    p._extract_topics(text, "")
    for topic in p._recent_topics:
        assert "\n" not in topic
        assert len(topic) <= 45
        assert topic == topic.strip()
    assert len(p._recent_topics) <= p._max_topics
