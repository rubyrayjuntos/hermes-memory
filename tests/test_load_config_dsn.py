"""load_config must not silently fall back to :5450 (#74 / D-MVP-5)."""
from __future__ import annotations

import pytest

from hermes_memory.config import dotenv_key_from_file, load_config, load_dotenv_files


def test_load_config_raises_when_dsn_unset(monkeypatch) -> None:
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.delenv("HYBRID_AGE_DSN", raising=False)
    with pytest.raises(RuntimeError, match="missing HYBRID_AGE_DSN"):
        load_config("/nonexistent/config.yaml")


def test_load_config_uses_explicit_env(monkeypatch) -> None:
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://hermes:x@127.0.0.1:5450/hermes_memory")
    monkeypatch.setenv("HYBRID_AGE_EXTRACT_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("HYBRID_AGE_EXTRACT_MODEL", "llama3.2:3b")
    monkeypatch.setenv("HYBRID_AGE_EXTRACT_MODE", "rules_then_model")
    cfg = load_config("/nonexistent/config.yaml")
    assert "5450" in cfg.dsn
    assert cfg.dsn.endswith("/hermes_memory")
    assert cfg.extract_url == "http://127.0.0.1:11434/v1"
    assert cfg.extract_model == "llama3.2:3b"
    assert cfg.extract_key_env == "HYBRID_AGE_EXTRACT_KEY"
    assert cfg.extract_mode == "rules_then_model"


def test_load_config_defaults_to_rules_only_and_rejects_unknown_mode(monkeypatch) -> None:
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.delenv("HYBRID_AGE_EXTRACT_MODE", raising=False)
    assert load_config("/nonexistent/config.yaml").extract_mode == "rules_only"

    monkeypatch.setenv("HYBRID_AGE_EXTRACT_MODE", "always")
    with pytest.raises(ValueError, match="invalid extract_mode"):
        load_config("/nonexistent/config.yaml")


def test_extract_model_schema_defaults_to_json_object(monkeypatch) -> None:
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.delenv("HYBRID_AGE_EXTRACT_MODEL_SCHEMA", raising=False)
    assert load_config("/nonexistent/config.yaml").extract_model_schema == "json_object"
    monkeypatch.setenv("HYBRID_AGE_EXTRACT_MODEL_SCHEMA", "json_schema")
    assert load_config("/nonexistent/config.yaml").extract_model_schema == "json_schema"
    monkeypatch.setenv("HYBRID_AGE_EXTRACT_MODEL_SCHEMA", "yaml")
    with pytest.raises(ValueError, match="invalid extract_model_schema"):
        load_config("/nonexistent/config.yaml")


def test_retrieval_mode_defaults_to_vector_and_validates_settings(monkeypatch) -> None:
    monkeypatch.setattr("hermes_memory.config.load_dotenv_files", lambda: None)
    monkeypatch.setattr("hermes_memory.config._load_yaml_block", lambda *a, **k: {})
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://localhost/hermes_memory")
    monkeypatch.delenv("HYBRID_AGE_RETRIEVAL_MODE", raising=False)
    monkeypatch.delenv("HYBRID_AGE_SHADOW_SAMPLE_RATE", raising=False)

    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.retrieval_mode == "vector"
    assert cfg.shadow_sample_rate == 0.2

    monkeypatch.setenv("HYBRID_AGE_RETRIEVAL_MODE", "shadow")
    monkeypatch.setenv("HYBRID_AGE_SHADOW_SAMPLE_RATE", "1.0")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.retrieval_mode == "shadow"
    assert cfg.shadow_sample_rate == 1.0

    monkeypatch.setenv("HYBRID_AGE_RETRIEVAL_MODE", "automatic")
    with pytest.raises(ValueError, match="invalid retrieval_mode"):
        load_config("/nonexistent/config.yaml")
    monkeypatch.setenv("HYBRID_AGE_RETRIEVAL_MODE", "vector")
    monkeypatch.setenv("HYBRID_AGE_SHADOW_SAMPLE_RATE", "1.1")
    with pytest.raises(ValueError, match="shadow_sample_rate"):
        load_config("/nonexistent/config.yaml")


def test_load_dotenv_files_clone_wins_over_hermes_home(tmp_path, monkeypatch) -> None:
    clone = tmp_path / "hermes-memory"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "hermes-memory"\n', encoding="utf-8")
    (clone / ".env").write_text(
        "HYBRID_AGE_DSN=postgres://hermes:dev@127.0.0.1:5450/hermes_memory\n",
        encoding="utf-8",
    )
    hermes = tmp_path / "home" / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / ".env").write_text(
        "HYBRID_AGE_DSN=postgres://hermes:inst@127.0.0.1:5452/hermes_memory_installed\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(clone)
    monkeypatch.setattr("hermes_memory.config.Path.home", lambda: tmp_path / "home")
    monkeypatch.delenv("HYBRID_AGE_DSN", raising=False)
    load_dotenv_files()
    import os
    assert "5450" in os.environ["HYBRID_AGE_DSN"]
    assert "hermes_memory_installed" not in os.environ["HYBRID_AGE_DSN"]


def test_dotenv_key_from_file_export_and_whitespace(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "  export HYBRID_AGE_DSN=postgres://hermes:x@127.0.0.1:5452/hermes_memory_installed\n"
        "OTHER=1\n",
        encoding="utf-8",
    )
    val = dotenv_key_from_file(path, "HYBRID_AGE_DSN")
    assert val is not None
    assert val.endswith("/hermes_memory_installed")
    assert dotenv_key_from_file(path, "MISSING") is None
