"""Schema head vs sql/migrations — catch live/test drift without a manual audit."""
from __future__ import annotations

from pathlib import Path

from hermes_memory.schema_guard import (
    LEGACY_NULL_EMBED_POLICY,
    list_expected_versions,
    missing_versions,
)


def test_expected_versions_include_every_on_disk_migration() -> None:
    """If this passes with a hardcoded list, a new V11 file would go unnoticed."""
    expected = list_expected_versions()
    disk = []
    root = Path(__file__).resolve().parents[1] / "sql" / "migrations"
    for p in sorted(root.glob("V*.sql")):
        disk.append(p.stem.split("__")[0])
    assert expected == disk
    assert "V8" in expected and "V9" in expected and "V10" in expected


def test_missing_versions_reports_gap_in_file_order() -> None:
    expected = ["V7", "V8", "V9", "V10"]
    assert missing_versions(["V1", "V7"], expected) == ["V8", "V9", "V10"]
    assert missing_versions(expected, expected) == []


def test_legacy_null_embed_policy_is_an_explicit_decision() -> None:
    """NULL embed_model is trusted nomic/768, not filtered, not backfilled."""
    assert LEGACY_NULL_EMBED_POLICY["decision"] == "trust_nomic_768"
    assert LEGACY_NULL_EMBED_POLICY["ann_excludes_null"] is False
    assert LEGACY_NULL_EMBED_POLICY["backfill"] is False
    store_src = (
        Path(__file__).resolve().parents[1] / "src" / "hermes_memory" / "store.py"
    ).read_text(encoding="utf-8")
    assert "trust_nomic_768" in store_src
    assert "Do not treat NULL as the live config default" in store_src


def test_tokenizer_slack_is_documented_as_hedge_not_measurement() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "hermes_memory" / "tokens.py").read_text(
        encoding="utf-8"
    )
    assert "hedge" in src.lower()
    assert "not a measured" in src.lower()


def test_provider_init_checks_schema_head() -> None:
    from hermes_memory import provider as provider_mod

    src = Path(provider_mod.__file__).read_text(encoding="utf-8")
    assert "require_schema_head" in src
