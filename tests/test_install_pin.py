"""Installer pins a git tree, stamps a SHA, and refuses the :5450 dev DSN."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_memory.install_cli import (
    INSTALLED_CONTAINER,
    INSTALLED_DB,
    INSTALLED_PORT,
    REPO_ROOT,
    VERSION_FILENAME,
    copy_plugin_tree,
    default_release_ref,
    export_pin,
    is_dev_clone_dsn,
    read_version_stamp,
    resolve_pin,
    write_installed_compose,
    write_version_stamp,
)


def test_is_dev_clone_dsn_matches_loopback_5450() -> None:
    assert is_dev_clone_dsn("postgres://hermes:x@localhost:5450/hermes_memory")
    assert is_dev_clone_dsn("postgres://hermes:x@127.0.0.1:5450/hermes_memory")
    assert not is_dev_clone_dsn("postgres://hermes:x@127.0.0.1:5452/hermes_memory_installed")
    assert not is_dev_clone_dsn("postgres://hermes:x@127.0.0.1:5450/hermes_test")


def test_version_stamp_roundtrip(tmp_path: Path) -> None:
    plugin = tmp_path / "hybrid-age"
    plugin.mkdir()
    write_version_stamp(plugin, sha="abc" * 8 + "abcd", ref="v0.1.0")
    got = read_version_stamp(plugin)
    assert got["sha"] == "abc" * 8 + "abcd"
    assert got["ref"] == "v0.1.0"
    assert "installed_at" in got
    assert (plugin / VERSION_FILENAME).is_file()


def test_copy_plugin_tree_is_a_copy_not_a_symlink(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "walk.py").write_text("def beam_score():\n    return 1\n", encoding="utf-8")
    plugin = tmp_path / "plugins" / "hybrid-age"
    copy_plugin_tree(src, plugin)
    assert plugin.is_dir()
    assert not plugin.is_symlink()
    assert (plugin / "walk.py").read_text(encoding="utf-8") == (src / "walk.py").read_text(
        encoding="utf-8"
    )
    (src / "walk.py").write_text("changed\n", encoding="utf-8")
    assert "beam_score" in (plugin / "walk.py").read_text(encoding="utf-8")


def test_default_release_ref_prefers_origin_main_not_v010(tmp_path: Path) -> None:
    """Do not use the CI checkout: PR jobs often lack origin/main."""
    import subprocess

    repo = tmp_path / "pin-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README").write_text("pin\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-c", "user.email=ci@example.test", "-c", "user.name=ci",
            "commit", "-m", "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=repo, check=True)
    subprocess.run(["git", "tag", "v0.1.0", head], cwd=repo, check=True)
    ref = default_release_ref(repo)
    assert ref == "origin/main"
    assert ref != "v0.1.0"
    _, sha = resolve_pin(repo, ref)
    _, v010 = resolve_pin(repo, "v0.1.0")
    assert sha == head


def test_live_checkout_default_ref_is_never_v010() -> None:
    assert default_release_ref(REPO_ROOT) != "v0.1.0"



def test_installed_compose_has_no_first_boot_init(tmp_path: Path) -> None:
    path = tmp_path / "docker-compose.installed.yml"
    write_installed_compose(path)
    text = path.read_text(encoding="utf-8")
    assert "sql/init" not in text
    assert "docker-entrypoint-initdb.d" not in text
    assert INSTALLED_CONTAINER in text
    assert INSTALLED_PORT in text
    assert INSTALLED_DB in text
    assert "hermes-memory-installed_pgdata" in text
    assert "pgdata:/var/lib/postgresql/data" in text


def test_resolve_and_export_pin_is_this_repo_head(tmp_path: Path) -> None:
    ref, sha = resolve_pin(REPO_ROOT, "HEAD")
    assert ref == "HEAD"
    assert len(sha) == 40
    dest = tmp_path / "export"
    export_pin(REPO_ROOT, sha, dest)
    assert (dest / "src" / "hermes_memory" / "walk.py").is_file()
    assert (dest / "sql" / "migrations").is_dir()


def test_checkout_preflight_accepts_worktree_git_file() -> None:
    from hermes_memory.install_cli import require_git_checkout

    require_git_checkout(REPO_ROOT)


def test_noncheckout_preflight_precedes_all_hermes_mutation(tmp_path, monkeypatch, capsys):
    import pytest

    from hermes_memory import install_cli

    repo = tmp_path / "wheel" / "site-packages"
    repo.mkdir(parents=True)
    home = tmp_path / "hermes-home"
    monkeypatch.setattr(install_cli, "REPO_ROOT", repo)
    monkeypatch.setattr(install_cli, "HERMES_HOME", home)
    monkeypatch.setattr(install_cli, "PLUGIN_DIRS", (home / "plugins" / "hybrid-age",))
    monkeypatch.setattr(install_cli, "CONFIG_PATHS", (home / "config.yaml",))
    with pytest.raises(SystemExit, match="clone"):
        install_cli.main(["--yes"])
    assert not home.exists()
    assert "HERMES_PG_PASSWORD=" not in capsys.readouterr().out


def test_install_records_owned_values_and_failure_status(tmp_path, monkeypatch):
    import hashlib
    import subprocess

    from hermes_memory import install_cli
    from hermes_memory.install_state import install_state_path, load_install_state

    home = tmp_path / "hermes-home"
    monkeypatch.setattr(install_cli, "HERMES_HOME", home)
    monkeypatch.setattr(install_cli, "PLUGIN_DIRS", (home / "plugins" / "hybrid-age",))
    monkeypatch.setattr(install_cli, "CONFIG_PATHS", (home / "config.yaml",))
    for key in ("HYBRID_AGE_DSN", "HYBRID_AGE_EMBED_URL", "HYBRID_AGE_EMBED_MODEL", "HYBRID_AGE_GRAPH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_PG_PASSWORD", "test-only-generated-value")
    # Fail the first external mutation (pip), after real local file writes/export.
    real_run = subprocess.run

    def runner(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        assert command[1:4] == ["-m", "pip", "install"]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="pip unavailable")

    monkeypatch.setattr(install_cli.subprocess, "run", runner)
    assert install_cli.main(["--yes", "--ref", "HEAD"]) == 1
    state = load_install_state(install_state_path(home))
    assert state.status == "incomplete"
    assert state.database_host == "127.0.0.1"
    assert state.database_port == 5452
    assert state.database_name == "hermes_memory_installed"
    assert state.database_user == "hermes"
    # pip failed before this invocation created any Compose artifacts.
    assert (state.compose_project, state.compose_container, state.compose_file) == (None, None, None)
    assert state.plugin_dirs == (str(home / "plugins" / "hybrid-age"),)
    assert state.owned_env_fingerprints["HERMES_PG_PASSWORD"] == hashlib.sha256(
        b"test-only-generated-value"
    ).hexdigest()
    text = install_state_path(home).read_text()
    assert "test-only-generated-value" not in text
    assert "postgres://" not in text


def test_successful_reused_target_records_no_compose_ownership(tmp_path, monkeypatch):
    import hashlib
    import subprocess
    import sys
    from types import SimpleNamespace

    from hermes_memory import install_cli
    from hermes_memory.install_state import install_state_path, load_install_state

    home = tmp_path / "hermes-home"
    monkeypatch.setattr(install_cli, "HERMES_HOME", home)
    monkeypatch.setattr(install_cli, "PLUGIN_DIRS", (home / "plugins" / "hybrid-age",))
    monkeypatch.setattr(install_cli, "CONFIG_PATHS", (home / "config.yaml",))
    for key in ("HYBRID_AGE_DSN", "HYBRID_AGE_EMBED_URL", "HYBRID_AGE_EMBED_MODEL", "HYBRID_AGE_GRAPH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(sys.modules, "hermes_memory.graph_api", SimpleNamespace(
        start_daemon=lambda: None, wait_ready=lambda **kwargs: True,
    ))
    real_run = subprocess.run

    def runner(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        assert command[0] != "docker"
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(install_cli.subprocess, "run", runner)
    dsn = "postgres://hermes:test-only-reused-value@127.0.0.1:5450/hermes_memory"
    assert install_cli.main(["--yes", "--ref", "HEAD", "--dsn", dsn, "--reuse-dsn"]) == 0
    state = load_install_state(install_state_path(home))
    assert state.status == "complete"
    assert state.database_port == 5450
    assert state.database_name == "hermes_memory"
    assert (state.compose_project, state.compose_container, state.compose_file) == (None, None, None)
    assert "HERMES_PG_PASSWORD" not in state.owned_env_fingerprints
    assert state.owned_env_fingerprints["HYBRID_AGE_DSN"] == hashlib.sha256(dsn.encode()).hexdigest()
    assert "test-only-reused-value" not in install_state_path(home).read_text()


@pytest.fixture
def isolated_install(tmp_path, monkeypatch):
    import subprocess
    import sys
    from types import SimpleNamespace

    from hermes_memory import install_cli

    home = tmp_path / "hermes-home"
    monkeypatch.setattr(install_cli, "HERMES_HOME", home)
    monkeypatch.setattr(install_cli, "PLUGIN_DIRS", (home / "plugins" / "hybrid-age",))
    monkeypatch.setattr(install_cli, "CONFIG_PATHS", (home / "config.yaml",))
    for key in ("HYBRID_AGE_DSN", "HYBRID_AGE_EMBED_URL", "HYBRID_AGE_EMBED_MODEL", "HYBRID_AGE_GRAPH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_PG_PASSWORD", "test-only-install-owned-password")
    monkeypatch.setitem(sys.modules, "hermes_memory.graph_api", SimpleNamespace(
        start_daemon=lambda: None, wait_ready=lambda **kwargs: True,
    ))
    real_run = subprocess.run

    def runner(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        output = "healthy" if command[:2] == ["docker", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(install_cli.subprocess, "run", runner)
    return install_cli, home


def test_failed_export_does_not_claim_untouched_existing_artifacts(isolated_install, monkeypatch):
    from hermes_memory.install_state import install_state_path, load_install_state

    cli, home = isolated_install
    plugin = home / "plugins" / "hybrid-age"
    skill = home / "skills" / "librarian-setup"
    plugin.mkdir(parents=True)
    skill.mkdir(parents=True)
    (plugin / "existing").write_text("user plugin")
    (skill / "existing").write_text("user skill")
    config = home / "config.yaml"
    config.write_text("user_setting: retained\n")

    def fail_export(repo, sha, dest):
        raise SystemExit("test export failure")

    monkeypatch.setattr(cli, "export_pin", fail_export)
    with pytest.raises(SystemExit, match="test export failure"):
        cli.main(["--yes", "--ref", "HEAD"])
    state = load_install_state(install_state_path(home))
    assert state.plugin_dirs == ()
    assert state.skill_dirs == ()
    assert state.config_paths == ()
    assert (state.compose_project, state.compose_container, state.compose_file) == (None, None, None)
    assert state.status == "incomplete"
    assert (plugin / "existing").read_text() == "user plugin"
    assert (skill / "existing").read_text() == "user skill"
    assert config.read_text() == "user_setting: retained\n"


def test_skipped_skill_source_does_not_claim_existing_skill(isolated_install, monkeypatch):
    from hermes_memory.install_state import install_state_path, load_install_state

    cli, home = isolated_install
    skill = home / "skills" / "librarian-setup"
    skill.mkdir(parents=True)
    (skill / "existing").write_text("user skill")

    def export_without_skill(repo, sha, dest):
        package = dest / "src" / "hermes_memory"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (dest / "docker").mkdir()
        (dest / "docker" / "Dockerfile").write_text("FROM scratch\n")

    monkeypatch.setattr(cli, "export_pin", export_without_skill)
    assert cli.main(["--yes", "--ref", "HEAD"]) == 0
    state = load_install_state(install_state_path(home))
    assert state.skill_dirs == ()
    assert state.plugin_dirs == (str(home / "plugins" / "hybrid-age"),)
    assert (skill / "existing").read_text() == "user skill"


def test_failed_reinstall_preserves_prior_artifact_ownership(isolated_install, monkeypatch):
    from hermes_memory.install_state import install_state_path, load_install_state

    cli, home = isolated_install
    assert cli.main(["--yes", "--ref", "HEAD"]) == 0
    previous = load_install_state(install_state_path(home))

    def fail_export(repo, sha, dest):
        raise SystemExit("test export failure")

    monkeypatch.setattr(cli, "export_pin", fail_export)
    with pytest.raises(SystemExit, match="test export failure"):
        cli.main(["--yes", "--ref", "HEAD"])
    current = load_install_state(install_state_path(home))
    assert current.plugin_dirs == previous.plugin_dirs
    assert current.skill_dirs == previous.skill_dirs
    assert current.config_paths == previous.config_paths
    assert current.compose_file == previous.compose_file


@pytest.mark.parametrize("edited_count", [0, 1, 2])
def test_reinstall_preserves_only_still_matching_password_ownership(
    isolated_install, edited_count,
):
    import hashlib

    from hermes_memory.install_state import install_state_path, load_install_state

    cli, home = isolated_install
    assert cli.main(["--yes", "--ref", "HEAD"]) == 0
    paths = cli.get_env_files()
    for path in paths[:edited_count]:
        path.write_text(path.read_text().replace(
            "HERMES_PG_PASSWORD=test-only-install-owned-password",
            "HERMES_PG_PASSWORD=test-only-user-edited-password",
        ))
    assert cli.main(["--yes", "--ref", "HEAD"]) == 0
    state = load_install_state(install_state_path(home))
    expected = hashlib.sha256(b"test-only-install-owned-password").hexdigest()
    if edited_count < 2:
        assert state.owned_env_fingerprints["HERMES_PG_PASSWORD"] == expected
    else:
        assert "HERMES_PG_PASSWORD" not in state.owned_env_fingerprints
    # These are the comparison inputs that value-sensitive cleanup consumes.
    # One shared hash continues to distinguish unchanged files from later edits.
    for index, path in enumerate(paths):
        values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        current_hash = hashlib.sha256(values["HERMES_PG_PASSWORD"].encode()).hexdigest()
        assert (current_hash == state.owned_env_fingerprints.get("HERMES_PG_PASSWORD")) == (
            index >= edited_count
        )
