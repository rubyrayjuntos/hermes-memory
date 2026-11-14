"""No-clone install: bundled assets, build-info pinning, and password handling."""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hermes_memory import bundled, install_cli

SHA = "a" * 40


def _bundle(tmp_path, info):
    bundle = tmp_path / "_bundle"
    bundle.mkdir()
    if info is not None:
        (bundle / "build_info.json").write_text(json.dumps(info), encoding="utf-8")
    return bundle


def test_bundle_pin_reads_the_commit_recorded_at_build(tmp_path):
    bundle = _bundle(tmp_path, {"sha": SHA, "ref": "v0.2.0", "dirty": False})
    assert bundled.resolve_bundle_pin(bundle) == ("v0.2.0", SHA)


def test_bundle_without_build_info_refuses_with_release_guidance(tmp_path):
    with pytest.raises(SystemExit, match="no bundled build information"):
        bundled.resolve_bundle_pin(_bundle(tmp_path, None))


@pytest.mark.parametrize("info, message", [
    ({"sha": "unknown", "ref": "x", "dirty": False}, "without a commit id"),
    ({"sha": SHA, "ref": "x", "dirty": True}, "uncommitted changes"),
])
def test_bundle_pin_refuses_unverifiable_builds(tmp_path, info, message):
    with pytest.raises(SystemExit, match=message):
        bundled.resolve_bundle_pin(_bundle(tmp_path, info))


def test_export_bundle_mirrors_repo_layout_so_migrate_finds_its_sql(tmp_path):
    bundle = _bundle(tmp_path, {"sha": SHA, "ref": "x", "dirty": False})
    (bundle / "docker").mkdir()
    (bundle / "docker" / "Dockerfile").write_text("FROM scratch\n")
    (bundle / "sql" / "migrations").mkdir(parents=True)
    (bundle / "sql" / "migrations" / "V1__x.sql").write_text("select 1;\n")
    (bundle / "scripts").mkdir()
    (bundle / "scripts" / "migrate.py").write_text("print('ok')\n")
    (bundle / "skills" / "librarian-setup").mkdir(parents=True)
    (bundle / "skills" / "librarian-setup" / "SKILL.md").write_text("# skill\n")
    dest = tmp_path / "export"
    bundled.export_bundle(dest, bundle)
    assert (dest / "docker" / "Dockerfile").is_file()
    assert (dest / "skills" / "librarian-setup" / "SKILL.md").is_file()
    # migrate.py resolves ``parents[1] / "sql" / "migrations"`` relative to itself.
    assert (dest / "scripts" / "migrate.py").parents[1].joinpath("sql", "migrations", "V1__x.sql").is_file()


def test_checkout_detection_follows_dot_git_only(tmp_path):
    assert not bundled.is_checkout(tmp_path)
    (tmp_path / ".git").mkdir()
    assert bundled.is_checkout(tmp_path)


def test_password_lookup_prefers_process_env_then_hermes_home_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_PG_PASSWORD=from-file\n")
    monkeypatch.setattr(install_cli, "get_env_files", lambda: (env_file,))
    monkeypatch.delenv("HERMES_PG_PASSWORD", raising=False)
    assert install_cli._existing_env_value("HERMES_PG_PASSWORD") == "from-file"
    monkeypatch.setenv("HERMES_PG_PASSWORD", "from-env")
    assert install_cli._existing_env_value("HERMES_PG_PASSWORD") == "from-env"


def test_release_gate_wheel_check_requires_bundled_assets():
    """The release gate must reject a wheel that cannot install without a clone."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    try:
        import release_gate
    finally:
        sys.path.pop(0)
    assert "hermes_memory/_bundle/build_info.json" in release_gate.REQUIRED_WHEEL_MEMBERS
    assert "hermes_memory/_bundle/docker/Dockerfile" in release_gate.REQUIRED_WHEEL_MEMBERS
    assert "hermes_memory/_bundle/scripts/migrate.py" in release_gate.REQUIRED_WHEEL_MEMBERS


def test_keep_db_uninstall_retains_the_password_its_volume_needs(tmp_path, monkeypatch):
    """--keep-db keeps the Postgres volume, so the password it was initialised with must survive."""
    import hashlib

    from hermes_memory.install_state import InstallState, install_state_path, write_install_state
    from hermes_memory.uninstall_cli import main

    monkeypatch.setitem(sys.modules, "hermes_memory.graph_api", types.SimpleNamespace(stop_daemon=lambda: None))
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_PG_PASSWORD=pw-retained\nKEEP_ME=1\n")
    state = InstallState(
        sha=SHA, ref="main", compose_project=None, compose_container=None, compose_file=None,
        database_host="127.0.0.1", database_port=5452, database_name="hermes_memory_installed",
        database_user="hermes", plugin_dirs=(), skill_dirs=(), config_paths=(),
        env_paths=(str(env_file),),
        owned_env_fingerprints={"HERMES_PG_PASSWORD": hashlib.sha256(b"pw-retained").hexdigest()},
        status="complete",
    )
    write_install_state(install_state_path(tmp_path), state)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    assert main(["--force", "--keep-db"], home=tmp_path, runner=runner) == 0
    assert "HERMES_PG_PASSWORD=pw-retained" in env_file.read_text()


def test_plugin_manifest_declares_the_same_dependencies_as_the_package():
    """Hermes admits pip_dependencies from plugin.yaml; they must match the package's own list."""
    import tomllib

    import yaml

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest = yaml.safe_load((root / "src" / "hermes_memory" / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "hybrid-age"
    assert sorted(manifest["pip_dependencies"]) == sorted(project["dependencies"])


def test_build_provenance_uses_the_pinned_commit_when_there_is_no_git(tmp_path):
    """Codex P2: a checkout install exports with git archive (no .git). The build must keep the
    commit the installer pinned, or the next install refuses the package it just made."""
    import hermes_build_info as build_meta

    sha = "b" * 40
    (tmp_path / build_meta.PIN_FILE).write_text(json.dumps({"sha": sha, "ref": "main"}), encoding="utf-8")
    assert build_meta.build_info(tmp_path) == {"sha": sha, "ref": "main", "dirty": False}
    assert install_cli.PIN_FILE == build_meta.PIN_FILE
    bare = tmp_path / "bare"
    bare.mkdir()
    assert build_meta.build_info(bare)["sha"] == "unknown"


def test_pin_file_keeps_dirty_state_so_an_sdist_from_a_dirty_tree_is_still_refused(tmp_path):
    """Codex P2: a dirty checkout's sdist must not become a clean-looking wheel."""
    import hermes_build_info as build_meta

    (tmp_path / build_meta.PIN_FILE).write_text(json.dumps({"sha": "c" * 40, "ref": "x", "dirty": True}), encoding="utf-8")
    assert build_meta.build_info(tmp_path)["dirty"] is True


def test_plugin_copy_excludes_cli_modules_that_fail_to_import_inside_hermes(tmp_path):
    """Hermes execs every module in the plugin directory. The CLI modules import hermes_memory by
    absolute name, which is absent there, so they are excluded from the copy."""
    src = tmp_path / "pkg"
    src.mkdir()
    for name in ("__init__.py", "provider.py", "install_cli.py", "uninstall_cli.py"):
        (src / name).write_text("# x\n", encoding="utf-8")
    dest = tmp_path / "plugin" / "hybrid-age"
    install_cli.copy_plugin_tree(src, dest)
    copied = {p.name for p in dest.iterdir()}
    assert {"__init__.py", "provider.py"} <= copied
    assert "install_cli.py" not in copied and "uninstall_cli.py" not in copied
