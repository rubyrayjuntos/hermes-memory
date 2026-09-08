"""load_config must not silently fall back to :5450 (#74 / D-MVP-5)."""
from __future__ import annotations

import pytest

from hermes_memory.config import load_config, load_dotenv_files


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
    cfg = load_config("/nonexistent/config.yaml")
    assert "5450" in cfg.dsn
    assert cfg.dsn.endswith("/hermes_memory")


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
