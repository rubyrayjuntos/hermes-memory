"""Installer pins a git tree, stamps a SHA, and refuses the :5450 dev DSN."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
    find_source_repo,
    is_dev_clone_dsn,
    is_git_repo,
    read_version_stamp,
    resolve_and_export_pin,
    resolve_pin,
    write_installed_compose,
    write_version_stamp,
    _flatten_github_tarball,
    _github_ref_name,
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


def test_default_release_ref_without_git_is_main(tmp_path: Path) -> None:
    assert default_release_ref(tmp_path) == "main"
    assert default_release_ref(None) == "main"


def test_github_ref_name_strips_origin_prefix() -> None:
    assert _github_ref_name("origin/main") == "main"
    assert _github_ref_name("main") == "main"
    assert _github_ref_name("v0.2.0") == "v0.2.0"


def test_flatten_github_tarball_hoists_single_top_dir(tmp_path: Path) -> None:
    top = tmp_path / "rubyrayjuntos-hermes-memory-abc1234"
    (top / "src" / "hermes_memory").mkdir(parents=True)
    (top / "src" / "hermes_memory" / "walk.py").write_text("ok\n", encoding="utf-8")
    _flatten_github_tarball(tmp_path)
    assert (tmp_path / "src" / "hermes_memory" / "walk.py").is_file()
    assert not top.exists()


def test_resolve_and_export_pin_head_without_checkout_exits() -> None:
    with pytest.raises(SystemExit) as exc:
        resolve_and_export_pin(None, "HEAD", Path("/tmp/unused"))
    assert "--ref HEAD needs a git checkout" in str(exc.value)


def test_resolve_and_export_pin_uses_github_when_no_repo(tmp_path: Path) -> None:
    dest = tmp_path / "export"

    def fake_resolve(ref: str) -> tuple[str, str]:
        assert ref == "main"
        return "main", "a" * 40

    def fake_export(sha: str, dest_path: Path) -> None:
        assert sha == "a" * 40
        dest_path.mkdir(parents=True, exist_ok=True)
        pkg = dest_path / "src" / "hermes_memory"
        pkg.mkdir(parents=True)
        (pkg / "walk.py").write_text("ok\n", encoding="utf-8")

    with patch("hermes_memory.install_cli.resolve_pin_github", side_effect=fake_resolve), patch(
        "hermes_memory.install_cli.export_pin_github", side_effect=fake_export
    ):
        ref, sha = resolve_and_export_pin(None, "main", dest)
    assert ref == "main"
    assert sha == "a" * 40
    assert (dest / "src" / "hermes_memory" / "walk.py").is_file()


def test_live_checkout_default_ref_is_never_v010() -> None:
    repo = find_source_repo() or REPO_ROOT
    assert default_release_ref(repo) != "v0.1.0"
    if is_git_repo(REPO_ROOT):
        assert default_release_ref(REPO_ROOT) != "HEAD" or not is_git_repo(REPO_ROOT)


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
    if not is_git_repo(REPO_ROOT):
        pytest.skip("not running from a hermes-memory git checkout")
    ref, sha = resolve_pin(REPO_ROOT, "HEAD")
    assert ref == "HEAD"
    assert len(sha) == 40
    dest = tmp_path / "export"
    export_pin(REPO_ROOT, sha, dest)
    assert (dest / "src" / "hermes_memory" / "walk.py").is_file()
    assert (dest / "sql" / "migrations").is_dir()
