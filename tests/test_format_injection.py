"""Injection grammar: path bound to seed header."""
from hermes_memory.provider import should_purge_concept
from hermes_memory.provider_helpers import format_injection


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
    assert block.startswith("<memory-context>")


def test_format_injection_empty():
    assert format_injection([]) == ""


def test_should_purge_c5_concepts():
    assert should_purge_concept("Project Zephyr") is True
    assert should_purge_concept("Atlas Vault Engine") is True
    assert should_purge_concept("Decide") is True
    assert should_purge_concept("Hermes Agent") is False
    assert should_purge_concept("Tokyo Eye") is False
