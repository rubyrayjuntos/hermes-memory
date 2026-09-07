"""Installer pins a git tree, stamps a SHA, and refuses the :5450 dev DSN."""
from __future__ import annotations

from pathlib import Path

from hermes_memory.install_cli import (
    REPO_ROOT,
    VERSION_FILENAME,
    copy_plugin_tree,
    default_release_ref,
    export_pin,
    is_dev_clone_dsn,
    read_version_stamp,
    resolve_pin,
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


def test_default_release_ref_is_origin_main_not_v010() -> None:
    ref = default_release_ref(REPO_ROOT)
    assert ref == "origin/main"
    assert ref != "v0.1.0"
    _, sha = resolve_pin(REPO_ROOT, ref)
    assert len(sha) == 40
    try:
        _, v010 = resolve_pin(REPO_ROOT, "v0.1.0")
    except SystemExit:
        return
    assert sha != v010


def test_resolve_and_export_pin_is_this_repo_head(tmp_path: Path) -> None:
    ref, sha = resolve_pin(REPO_ROOT, "HEAD")
    assert ref == "HEAD"
    assert len(sha) == 40
    dest = tmp_path / "export"
    export_pin(REPO_ROOT, sha, dest)
    assert (dest / "src" / "hermes_memory" / "walk.py").is_file()
    assert (dest / "sql" / "migrations").is_dir()
