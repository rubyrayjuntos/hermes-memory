"""Injection grammar: path bound to seed header."""
import re

from hermes_memory.provider import should_purge_concept
from hermes_memory.provider_helpers import format_injection

# Hermes agent.memory_manager.sanitize_context — copied as a contract test.
# Do not wrap prefetch in hyphen <memory-context> tags.
_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)


def _hermes_sanitize(text: str) -> str:
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text


def test_format_injection_binds_path_to_seed():
    block = format_injection([
        {
            "score": 0.64,
            "content": "Phase 2 install plan",
            "paths": [{"triple": "[Turn hello] -ABOUT-> [Concept Hermes Agent] w=0.91 c=0.88"}],
        },
        {
            "score": 0.61,
            "content": "schema notes",
            "paths": [],
        },
    ])
    assert "[SEED V1 64%] [Path: [Turn hello] -ABOUT-> [Concept Hermes Agent] w=0.91 c=0.88]" in block
    assert "[SEED V2 61%] [Path: none]" in block
    assert "Phase 2 install plan" in block
    assert "<memory-context>" not in block
    assert "</memory-context>" not in block
    assert "<memory_context>" not in block


def test_format_injection_survives_hermes_sanitize():
    block = format_injection([
        {
            "score": 0.64,
            "content": "Tokyo Eye triple-synch",
            "paths": [{"triple": "[Turn x] -ABOUT-> [Concept Tokyo Eye] w=0.91 c=0.88"}],
        },
    ])
    clean = _hermes_sanitize(block)
    assert "[SEED V1 64%]" in clean
    assert "[Path: [Turn x] -ABOUT-> [Concept Tokyo Eye] w=0.91 c=0.88]" in clean
    assert "Tokyo Eye triple-synch" in clean


def test_hyphen_fence_is_nuked_by_hermes_sanitize():
    wrapped = (
        "<memory-context>\n"
        "[SEED V1 64%] [Path: none]\n"
        "excerpt\n"
        "</memory-context>\n"
    )
    assert _hermes_sanitize(wrapped).strip() == ""


def test_format_injection_empty():
    assert format_injection([]) == ""


def test_should_purge_c5_concepts():
    assert should_purge_concept("Project Zephyr") is True
    assert should_purge_concept("Atlas Vault Engine") is True
    assert should_purge_concept("Decide") is True
    assert should_purge_concept("Hermes Agent") is False
    assert should_purge_concept("Tokyo Eye") is False
