"""Spec 4 T4.1: extract_assistant_replies config flag (Spec 4 §4.3)."""
from hermes_memory.config import load_config


def test_extract_assistant_replies_defaults_false(monkeypatch):
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.delenv("HYBRID_AGE_EXTRACT_ASSISTANT_REPLIES", raising=False)
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.extract_assistant_replies is False


def test_extract_assistant_replies_settable_via_env(monkeypatch):
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.setenv("HYBRID_AGE_EXTRACT_ASSISTANT_REPLIES", "true")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.extract_assistant_replies is True

    monkeypatch.setenv("HYBRID_AGE_EXTRACT_ASSISTANT_REPLIES", "false")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.extract_assistant_replies is False
