"""Installation ownership records are strict, atomic, and contain no credentials."""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest


def state_module():
    # Import in the test so RED records every missing contract independently.
    return importlib.import_module("hermes_memory.install_state")


def example_state(home: Path):
    return state_module().InstallState(
        sha="a" * 40,
        ref="origin/main",
        compose_project="hermes-memory-installed",
        compose_container="hermes-memory-postgres-installed",
        compose_file=str(home / "compose" / "hermes-memory-installed.yml"),
        database_host="127.0.0.1",
        database_port=5452,
        database_name="hermes_memory_installed",
        database_user="hermes",
        plugin_dirs=(str(home / "plugins" / "hybrid-age"),),
        skill_dirs=(str(home / "skills" / "librarian-setup"),),
        config_paths=(str(home / "config.yaml"),),
        env_paths=(str(home / ".env"),),
        owned_env_fingerprints={
            "HYBRID_AGE_DSN": "b" * 64,
            "HERMES_PG_PASSWORD": "c" * 64,
        },
        status="installing",
    )


def test_atomic_private_roundtrip_contains_only_comparison_metadata(tmp_path, monkeypatch):
    module = state_module()
    state = example_state(tmp_path)
    path = module.install_state_path(tmp_path)
    assert path == tmp_path / "hermes-memory-install.json"
    assert module.load_install_state(path) is None
    module.write_install_state(path, state)
    original = path.read_bytes()
    real_replace = os.replace

    def observe_replace(source, target):
        assert path.read_bytes() == original
        assert Path(source).parent == path.parent
        assert Path(source).stat().st_mode & 0o777 == 0o600
        assert json.loads(Path(source).read_text())["status"] == "complete"
        real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", observe_replace)
    completed = replace(state, status="complete")
    module.write_install_state(path, completed)
    assert module.load_install_state(path) == completed
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(tmp_path.iterdir()) == {path}
    payload = json.loads(path.read_text())
    assert payload["owned_env_fingerprints"] == {
        "HYBRID_AGE_DSN": "b" * 64,
        "HERMES_PG_PASSWORD": "c" * 64,
    }
    assert "postgres://" not in path.read_text()


def test_failed_replace_retains_previous_record_and_removes_temp(tmp_path, monkeypatch):
    module = state_module()
    path = module.install_state_path(tmp_path)
    state = example_state(tmp_path)
    module.write_install_state(path, state)

    def fail_replace(source, target):
        raise OSError("replacement failed")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        module.write_install_state(path, replace(state, status="complete"))
    assert module.load_install_state(path) == state
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("patch", [
    {"schema_version": 2}, {"schema_version": True}, {"sha": "not-a-sha"},
    {"database_port": 0}, {"database_port": True}, {"database_port": 65536},
    {"database_host": "postgres://credentials@localhost"},
    {"database_name": ""}, {"plugin_dirs": ["relative/path"]},
    {"plugin_dirs": ["/tmp/../etc"]}, {"env_paths": "not-a-list"},
    {"compose_file": None}, {"status": "unknown"},
    {"owned_env_fingerprints": {"HERMES_PG_PASSWORD": "plaintext"}},
    {"owned_env_fingerprints": {"UNRELATED_SECRET": "a" * 64}},
    {"password": "must-not-be-accepted"}, {"dsn": "must-not-be-accepted"},
])
def test_rejects_invalid_record_without_echoing_values(tmp_path, patch):
    module = state_module()
    payload = asdict(example_state(tmp_path))
    payload.update(patch)
    path = module.install_state_path(tmp_path)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError) as exc:
        module.load_install_state(path)
    assert "must-not-be-accepted" not in str(exc.value)
    assert "plaintext" not in str(exc.value)


@pytest.mark.parametrize("text", ["{", "null", "[]", '{}', '{"schema_version":1,"schema_version":1}'])
def test_rejects_corrupt_or_incomplete_json(tmp_path, text):
    module = state_module()
    path = module.install_state_path(tmp_path)
    path.write_text(text)
    with pytest.raises(ValueError):
        module.load_install_state(path)


def test_writer_validates_before_replacing_record(tmp_path):
    module = state_module()
    path = module.install_state_path(tmp_path)
    state = example_state(tmp_path)
    module.write_install_state(path, state)
    with pytest.raises(ValueError):
        module.write_install_state(path, replace(state, database_port=-1))
    assert module.load_install_state(path) == state
