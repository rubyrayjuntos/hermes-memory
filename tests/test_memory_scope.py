"""MVP memory scope: one shared memory per profile by default, per-session on request."""

from hermes_memory import config as cfg_mod
from hermes_memory.config import SHARED_AGENT_IDENTITY, resolve_agent_identity

GATEWAY = {"gateway_session_key": "agent:main:local:dm:chat-1", "agent_identity": "agent:main:local:dm:chat-1"}


def test_shared_scope_ignores_the_chat_session_identity():
    assert resolve_agent_identity("shared", GATEWAY) == SHARED_AGENT_IDENTITY
    assert resolve_agent_identity("shared", {}) == SHARED_AGENT_IDENTITY


def test_session_scope_keeps_per_chat_identity():
    assert resolve_agent_identity("session", GATEWAY) == "agent:main:local:dm:chat-1"
    assert resolve_agent_identity("session", {"agent_identity": "default"}) == SHARED_AGENT_IDENTITY


def test_default_scope_is_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgresql://u:p@127.0.0.1:5452/hermes_memory_installed")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cfg_mod.HybridAgeConfig().memory_scope == "shared"
    assert cfg_mod.load_config(str(tmp_path / "missing.yaml")).memory_scope == "shared"


def test_yaml_selects_session_scope_and_rejects_unknown_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgresql://u:p@127.0.0.1:5452/hermes_memory_installed")
    path = tmp_path / "config.yaml"
    path.write_text("hybrid_age:\n  memory_scope: session\n", encoding="utf-8")
    assert cfg_mod.load_config(str(path)).memory_scope == "session"
    path.write_text("hybrid_age:\n  memory_scope: everyone\n", encoding="utf-8")
    assert cfg_mod.load_config(str(path)).memory_scope == "shared"
