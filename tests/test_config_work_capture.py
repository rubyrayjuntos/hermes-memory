"""Spec 5 T5.1/T5.2: work_capture config flag."""
from hermes_memory.config import load_config


def test_work_capture_defaults_false(monkeypatch):
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.delenv("HYBRID_AGE_WORK_CAPTURE", raising=False)
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.work_capture is False


def test_work_capture_settable_via_env(monkeypatch):
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.setenv("HYBRID_AGE_WORK_CAPTURE", "true")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.work_capture is True

    monkeypatch.setenv("HYBRID_AGE_WORK_CAPTURE", "false")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.work_capture is False
