"""Schema head vs sql/migrations — catch live/test drift without a manual audit."""
from __future__ import annotations

from pathlib import Path

from hermes_memory.schema_guard import (
    DEPLOY_TOPOLOGY,
    LEGACY_NULL_EMBED_POLICY,
    LIVE_EVAL_POLICY,
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
    # Older code vs a newer applied head: extra versions are not a failure.
    assert missing_versions(["V7", "V8", "V9", "V10", "V11"], expected) == []


def test_github_cd_does_not_migrate_live() -> None:
    """GHA cannot reach 127.0.0.1:5450. Cutover migrate is process start, not CD."""
    cd = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cd.yml").read_text(
        encoding="utf-8"
    )
    assert "migrate.py" not in cd
    assert DEPLOY_TOPOLOGY["github_cd_migrates_live"] is False
    assert DEPLOY_TOPOLOGY["process_start_applies_migrations"] is True
    assert DEPLOY_TOPOLOGY["require_schema_head_is_backstop"] is True
    assert DEPLOY_TOPOLOGY["rolling_deploy"] is False
    assert DEPLOY_TOPOLOGY["head_check"] == "expected_not_applied_only"
    block = Path(
        __import__("hermes_memory.schema_guard", fromlist=["schema_guard"]).__file__
    ).read_text(encoding="utf-8")
    topo = block[block.find("DEPLOY_TOPOLOGY"): block.find("LIVE_EVAL_POLICY")]
    assert topo.index('"rolling_deploy"') < topo.index('"head_check"')


def test_provider_applies_migrations_before_schema_head() -> None:
    from hermes_memory import graph_api as graph_mod
    from hermes_memory import provider as provider_mod

    for mod in (provider_mod, graph_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        apply_at = src.find("apply_pending_migrations")
        head_at = src.find("require_schema_head")
        assert apply_at != -1, f"{mod.__name__} must apply migrations at boot"
        assert head_at != -1
        assert apply_at < head_at, f"{mod.__name__} must migrate before the head check"


def test_live_eval_waits_for_v9_gap_backfill() -> None:
    """Do not lock a live-shaped golden set on the 257 unpassported turns."""
    assert LIVE_EVAL_POLICY["backfill_v9_gap_before_golden"] is True
    assert LIVE_EVAL_POLICY["exclude_unpassported_turns_until_backfill"] is True
    assert LIVE_EVAL_POLICY["enforced"] is True
    golden = (
        Path(__file__).resolve().parents[1] / "tests" / "data" / "ann_golden_turn_ids.json"
    ).read_text(encoding="utf-8")
    assert "golden-ann-" in golden
    assert '"eval_kind": "synthetic"' in golden


def test_live_shaped_golden_is_refused_until_backfill() -> None:
    from hermes_memory.schema_guard import assert_live_shaped_eval_allowed

    assert_live_shaped_eval_allowed("synthetic", unpassported_count=257)
    assert_live_shaped_eval_allowed("live_shaped", unpassported_count=0)
    try:
        assert_live_shaped_eval_allowed("live_shaped", unpassported_count=257)
    except RuntimeError as exc:
        assert "backfill" in str(exc).lower()
    else:
        raise AssertionError("live_shaped eval must refuse while unpassported_count > 0")
    try:
        assert_live_shaped_eval_allowed("live_shaped", unpassported_count=None)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unknown passport gap must fail closed")


def test_bench_load_golden_set_refuses_live_shaped(tmp_path) -> None:
    from hermes_memory.bench.cli import load_golden_set

    path = tmp_path / "live.json"
    path.write_text(
        '{"eval_kind": "live_shaped", "cases": [{"id": "q", "query": "x", "expected_memory": "y"}]}',
        encoding="utf-8",
    )
    try:
        load_golden_set(str(path))
    except RuntimeError as exc:
        assert "backfill" in str(exc).lower()
    else:
        raise AssertionError("bench must not load a live_shaped set while the gap is unknown")


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
