import subprocess
import sys
import types
from dataclasses import replace

import pytest

from hermes_memory.install_state import InstallState, install_state_path, write_install_state
from hermes_memory.uninstall_cli import main


@pytest.fixture
def state(tmp_path):
    return InstallState(
        sha="a" * 40, ref="main", compose_project="hermes-memory",
        compose_container="hermes-db", compose_file=str(tmp_path / "compose.yml"),
        database_host="127.0.0.1", database_port=5452,
        database_name="hermes_memory_installed", database_user="hermes",
        plugin_dirs=(), skill_dirs=(), config_paths=(), env_paths=(),
        owned_env_fingerprints={}, status="incomplete",
    )


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_memory.graph_api", types.SimpleNamespace(stop_daemon=lambda: None))
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    return commands, runner


def invoke(home, calls, *args):
    commands, runner = calls
    result = main(["--force", *args], home=home, runner=runner)
    return result, [command for command, _ in commands]


def test_missing_record_fails_before_any_actions(tmp_path, calls):
    result, commands = invoke(tmp_path, calls)
    assert result != 0
    assert commands == []


def test_corrupt_record_fails_before_any_actions(tmp_path, calls):
    install_state_path(tmp_path).write_text('{broken')
    result, commands = invoke(tmp_path, calls)
    assert result != 0
    assert commands == []


def test_keep_db_without_record_allows_non_database_cleanup(tmp_path, calls):
    result, commands = invoke(tmp_path, calls, "--keep-db")
    assert result == 0
    assert not any(cmd[0] in ("dropdb", "docker") for cmd in commands)


def test_recorded_owned_stack_removal_uses_volumes_not_dropdb(tmp_path, calls, state):
    write_install_state(install_state_path(tmp_path), state)
    result, commands = invoke(tmp_path, calls)
    assert result == 0
    assert ["docker", "compose", "-p", "hermes-memory", "-f", state.compose_file, "down", "--volumes"] in commands
    assert not any(cmd[0] == "dropdb" for cmd in commands)


def test_keep_db_stops_owned_stack_without_volumes(tmp_path, calls, state):
    write_install_state(install_state_path(tmp_path), state)
    result, commands = invoke(tmp_path, calls, "--keep-db")
    assert result == 0
    assert ["docker", "compose", "-p", "hermes-memory", "-f", state.compose_file, "down"] in commands
    assert not any(cmd[0] == "dropdb" for cmd in commands)


def test_explicit_different_dsn_does_not_remove_owned_stack(tmp_path, calls, state, capsys):
    write_install_state(install_state_path(tmp_path), state)
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://other:secret@db.example:6543/external")
    assert result == 0
    assert ["dropdb", "-h", "db.example", "-p", "6543", "-U", "other", "--", "external"] in commands
    assert not any(cmd[0] == "docker" for cmd in commands)
    assert "secret" not in capsys.readouterr().out


def test_recorded_external_database_uses_installed_port(tmp_path, calls, state):
    state = replace(state, compose_project=None, compose_container=None, compose_file=None)
    write_install_state(install_state_path(tmp_path), state)
    result, commands = invoke(tmp_path, calls)
    assert result == 0
    assert ["dropdb", "-h", "127.0.0.1", "-p", "5452", "-U", "hermes", "--", "hermes_memory_installed"] in commands


def test_explicit_dsn_without_record_is_used(tmp_path, calls):
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://alice@db:5432/mydb")
    assert result == 0
    assert ["dropdb", "-h", "db", "-p", "5432", "-U", "alice", "--", "mydb"] in commands


def test_explicit_matching_dsn_removes_recorded_volume(tmp_path, calls, state):
    write_install_state(install_state_path(tmp_path), state)
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://hermes@127.0.0.1:5452/hermes_memory_installed")
    assert result == 0
    assert ["docker", "compose", "-p", "hermes-memory", "-f", state.compose_file, "down", "--volumes"] in commands
    assert not any(cmd[0] == "dropdb" for cmd in commands)


def test_compose_failure_stops_uninstall(tmp_path, state, monkeypatch):
    write_install_state(install_state_path(tmp_path), state)
    commands = []
    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "failure")
    assert main(["--force"], home=tmp_path, runner=runner) == 1
    assert len(commands) == 1 and commands[0][0] == "docker"


def test_invalid_dsn_never_discloses_password(tmp_path, calls, capsys):
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://user:secret@host:wrong/db")
    assert result != 0
    assert commands == []
    assert "secret" not in capsys.readouterr().out


def test_explicit_dsn_with_unhandled_connection_options_is_rejected(tmp_path, calls):
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://alice@db:5432/mydb?hostaddr=192.0.2.1")
    assert result != 0
    assert commands == []


def test_corrupt_record_allows_explicit_target_without_compose(tmp_path, calls):
    install_state_path(tmp_path).write_text('{broken')
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://alice@db:5432/mydb")
    assert result == 0
    assert ["dropdb", "-h", "db", "-p", "5432", "-U", "alice", "--", "mydb"] in commands
    assert not any(cmd[0] == "docker" for cmd in commands)


def test_corrupt_record_allows_keep_db_cleanup_without_compose(tmp_path, calls):
    install_state_path(tmp_path).write_text('{broken')
    result, commands = invoke(tmp_path, calls, "--keep-db")
    assert result == 0
    assert not any(cmd[0] in ("dropdb", "docker") for cmd in commands)


def test_dropdb_ignores_ambient_connection_routing(tmp_path, calls, monkeypatch):
    for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGDATABASE", "PGUSER",
                 "PGPORT", "PGHOST", "PGOPTIONS", "PGPASSWORD"):
        monkeypatch.setenv(name, "ambient-value")
    result, commands = invoke(tmp_path, calls, "--dsn", "postgresql://alice:intended@db:5432/mydb")
    assert result == 0
    drop = next(kwargs for command, kwargs in calls[0] if command[0] == "dropdb")
    env = drop["env"]
    assert env["PGPASSWORD"] == "intended"
    assert not any(name in env for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGDATABASE",
                                             "PGUSER", "PGPORT", "PGHOST", "PGOPTIONS"))


def test_dropdb_discards_ambient_password_when_dsn_has_none(tmp_path, calls, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "ambient-value")
    result, _ = invoke(tmp_path, calls, "--dsn", "postgresql://alice@db:5432/mydb")
    assert result == 0
    env = next(kwargs["env"] for command, kwargs in calls[0] if command[0] == "dropdb")
    assert "PGPASSWORD" not in env


def test_owned_artifacts_removed_by_default(tmp_path, calls, state):
    plugin = tmp_path / 'plugins' / 'hybrid-age'
    skill = tmp_path / 'skills' / 'librarian-setup'
    plugin.mkdir(parents=True)
    skill.mkdir(parents=True)
    state = replace(state, plugin_dirs=(str(plugin),), skill_dirs=(str(skill),))
    write_install_state(install_state_path(tmp_path), state)
    result, _ = invoke(tmp_path, calls, '--keep-db')
    assert result == 0
    assert not plugin.exists() and not skill.exists()
    assert not install_state_path(tmp_path).exists()


def test_keep_plugin_preserves_plugin_and_skill(tmp_path, calls, state):
    plugin = tmp_path / 'plugins' / 'hybrid-age'
    skill = tmp_path / 'skills' / 'librarian-setup'
    plugin.mkdir(parents=True)
    skill.mkdir(parents=True)
    write_install_state(install_state_path(tmp_path), replace(state, plugin_dirs=(str(plugin),), skill_dirs=(str(skill),)))
    result, _ = invoke(tmp_path, calls, '--keep-db', '--keep-plugin')
    assert result == 0
    assert plugin.exists() and skill.exists()


def test_untrusted_owned_path_is_not_removed_and_retains_state(tmp_path, calls, state, capsys):
    victim = tmp_path / 'other'
    victim.mkdir()
    write_install_state(install_state_path(tmp_path), replace(state, plugin_dirs=(str(victim),)))
    result, _ = invoke(tmp_path, calls, '--keep-db')
    assert result != 0 and victim.exists() and install_state_path(tmp_path).exists()
    assert 'Incomplete' in capsys.readouterr().out


def test_required_failure_retains_state_and_reports_incomplete(tmp_path, state, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, 'hermes_memory.graph_api', types.SimpleNamespace(stop_daemon=lambda: None))
    write_install_state(install_state_path(tmp_path), state)
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[0] != 'docker' else 0, '', '')
    assert main(['--force', '--keep-db'], home=tmp_path, runner=runner) != 0
    assert install_state_path(tmp_path).exists()
    assert 'Incomplete' in capsys.readouterr().out


def test_refusal_reports_incomplete(tmp_path, calls, capsys):
    result, _ = invoke(tmp_path, calls)
    assert result != 0
    assert 'Incomplete' in capsys.readouterr().out


@pytest.mark.parametrize('category,name', [('plugins', 'hybrid-age'), ('skills', 'librarian-setup')])
@pytest.mark.parametrize('outside', [False, True])
def test_parent_symlink_redirect_refused(tmp_path, calls, state, category, name, outside):
    home = tmp_path / 'home'
    home.mkdir()
    destination = (tmp_path / 'outside' if outside else home / 'unrelated')
    victim = destination / name
    victim.mkdir(parents=True)
    (home / category).symlink_to(destination, target_is_directory=True)
    field = 'plugin_dirs' if category == 'plugins' else 'skill_dirs'
    state = replace(state, **{field: (str(home / category / name),)})
    write_install_state(install_state_path(home), state)
    result, _ = invoke(home, calls, '--keep-db')
    assert result != 0
    assert victim.is_dir()
    assert install_state_path(home).exists()
