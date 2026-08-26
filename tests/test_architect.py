"""tests/test_architect.py — coverage for scripts/architect.py (Issue #4).

Validates:
- kill switch (HERMES_AGENT_ENABLED gate)
- dry-run (no side-effects)
- file generation (not string patch)
- EXPLAIN pre-flight (mocked connection)
- hits.jsonl logger + pin_weight/ttl boost
- webhook notification (dry-run + no-URL cases)
- CODEOWNERS + .env.example documentation expectations
- V3 audit compatibility helpers (structure)
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import architect  # noqa: E402


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_disabled_by_default(self):
        assert architect.is_agent_enabled({}) is False

    def test_enabled_truthy_values(self):
        for v in ("1", "true", "True", "YES", "on", "enabled", "ON"):
            assert architect.is_agent_enabled({"HERMES_AGENT_ENABLED": v}) is True, v

    def test_disabled_falsy_values(self):
        for v in ("0", "false", "no", "off", "", "random"):
            assert architect.is_agent_enabled({"HERMES_AGENT_ENABLED": v}) is False, v

    def test_require_enabled_force_bypasses(self):
        assert architect.require_enabled(force=True) is True
        # even when env is false, force wins
        with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "false"}):
            assert architect.require_enabled(force=True) is True

    def test_require_enabled_respects_env(self):
        with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "0"}, clear=False):
            assert architect.require_enabled(force=False) is False
        with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "1"}, clear=False):
            assert architect.require_enabled(force=False) is True

    def test_main_exits_zero_when_disabled(self):
        with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "false"}, clear=False):
            rc = architect.main(["--branch", "x", "--title", "t"])
            assert rc == 0

    def test_run_architect_noop_when_disabled(self):
        with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "false"}, clear=False):
            rc = architect.run_architect(branch="x", title="t")
            assert rc == 0

    def test_main_check_enabled_flag(self, capsys):
        rc = architect.main(["--check-enabled"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "HERMES_AGENT_ENABLED=" in out


# ---------------------------------------------------------------------------
# File generation (not string patch)
# ---------------------------------------------------------------------------

class TestFileGeneration:
    def test_generate_file_writes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.txt"
            res = architect.generate_file(p, "hello world", dry_run=False)
            assert res["written"] is True
            assert p.read_text() == "hello world"
            assert "checksum" in res

    def test_generate_file_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.txt"
            res = architect.generate_file(p, "hello", dry_run=True)
            assert res["written"] is False
            assert res["dry_run"] is True
            assert not p.exists()

    def test_generate_migration_file(self):
        with tempfile.TemporaryDirectory() as td:
            md = Path(td)
            res = architect.generate_migration_file(md, "V4", "add feature", "CREATE TABLE foo (id int);")
            assert res["written"] is True
            assert (md / "V4__add_feature.sql").exists()
            assert "architect.py" in (md / "V4__add_feature.sql").read_text()

    def test_generate_migration_invalid_version(self):
        with tempfile.TemporaryDirectory() as td:
            with pytest.raises(ValueError):
                architect.generate_migration_file(Path(td), "bad", "desc", "SELECT 1;")

    def test_generate_migration_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            md = Path(td)
            res = architect.generate_migration_file(md, "V5", "test", "SELECT 1;", dry_run=True)
            assert res["dry_run"] is True
            assert not (md / "V5__test.sql").exists()

    def test_generate_code_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gen.py"
            tpl = "hello {name} v{version}"
            res = architect.generate_code_artifact(p, tpl, {"name": "world", "version": "2"}, dry_run=False)
            assert p.read_text() == "hello world v2"
            assert res["written"] is True

    def test_generate_code_artifact_missing_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gen.py"
            with pytest.raises(KeyError):
                architect.generate_code_artifact(p, "hi {missing}", {}, dry_run=False)


# ---------------------------------------------------------------------------
# EXPLAIN pre-flight
# ---------------------------------------------------------------------------

class TestExplainPreflight:
    def _mock_conn(self, plan_rows):
        cur = MagicMock()
        cur.fetchall.return_value = plan_rows
        # context manager
        cm = MagicMock()
        cm.__enter__.return_value = cur
        cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cm
        return conn

    def test_explain_returns_plan(self):
        conn = self._mock_conn([("Seq Scan on foo  (cost=0.00..10.00 rows=100)",)])
        res = architect.explain_preflight(conn, "SELECT * FROM foo;")
        assert res["ok"] is True
        assert "Seq Scan" in res["plan"]
        assert len(res["warnings"]) == 1  # seq scan warning

    def test_explain_index_scan_no_warning(self):
        conn = self._mock_conn([("Index Scan using idx_foo on foo  (cost=0.00..5.00)",)])
        res = architect.explain_preflight(conn, "SELECT * FROM foo WHERE id=1;")
        assert res["ok"] is True
        assert res["warnings"] == []

    def test_explain_failure_handled(self):
        cur = MagicMock()
        cur.execute.side_effect = Exception("connection failed")
        cm = MagicMock()
        cm.__enter__.return_value = cur
        cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cm
        res = architect.explain_preflight(conn, "SELECT 1;")
        assert res["ok"] is False
        assert "EXPLAIN failed" in res["warnings"][0]

    def test_explain_preflight_for_migration(self):
        # DDL should be skipped, SELECT should be explained
        conn = self._mock_conn([("Index Scan on bar",)])
        sql = "CREATE TABLE foo (id int); SELECT * FROM bar WHERE id=1; INSERT INTO bar VALUES (1);"
        results = architect.explain_preflight_for_migration(conn, sql)
        assert len(results) == 3
        assert results[0].get("skipped") is True  # CREATE TABLE
        assert results[1].get("ok") is True       # SELECT


# ---------------------------------------------------------------------------
# Pin-memory weighted boost
# ---------------------------------------------------------------------------

class TestPinBoost:
    def test_no_pin_weight_returns_base(self):
        assert architect.compute_pin_boost(0.8, pin_weight=0.0) == pytest.approx(0.8)

    def test_fresh_pin_boost(self):
        # base 1.0, weight 1.0, ttl 86400, age 0 => 1 * (1 + 1*1) = 2.0
        assert architect.compute_pin_boost(1.0, pin_weight=1.0, ttl_seconds=86400, age_seconds=0) == pytest.approx(2.0)

    def test_decay(self):
        # at age == ttl, decay = 1/e
        v0 = architect.compute_pin_boost(1.0, pin_weight=1.0, ttl_seconds=100, age_seconds=0)
        v1 = architect.compute_pin_boost(1.0, pin_weight=1.0, ttl_seconds=100, age_seconds=100)
        assert v1 < v0
        assert v1 == pytest.approx(1 + math.exp(-1))

    def test_ttl_zero_no_decay(self):
        v0 = architect.compute_pin_boost(1.0, pin_weight=0.5, ttl_seconds=0, age_seconds=999999)
        assert v0 == pytest.approx(1.5)

    def test_negative_age_clamped(self):
        v = architect.compute_pin_boost(1.0, pin_weight=1.0, ttl_seconds=100, age_seconds=-50)
        assert v == pytest.approx(2.0)

    def test_apply_pin_boost_sorts(self):
        cands = [
            {"id": "a", "score": 0.9, "pinned": False},
            {"id": "b", "score": 0.5, "pinned": True, "pin_weight": 2.0, "pinned_at": 0, "ttl": 86400},
        ]
        # b boosted: 0.5 * (1+2*decay) ; with pinned_at 0 and now ~now, decay tiny, but weight still boosts
        # Use now=0 so decay=1.0
        out = architect.apply_pin_boost(cands, pin_weight=2.0, ttl_seconds=86400, now=0)
        # b at now=0: 0.5*(1+2)=1.5 should rank above a=0.9
        assert out[0]["id"] == "b"
        assert out[0]["boosted_score"] == pytest.approx(1.5)
        assert out[1]["id"] == "a"

    def test_apply_pin_boost_iso_timestamp(self):
        # pinned_at as ISO string
        import datetime
        now = 1700000000.0
        pinned_at_iso = datetime.datetime.fromtimestamp(now - 100, tz=datetime.timezone.utc).isoformat()
        cands = [{"id": "x", "score": 1.0, "pinned": True, "pinned_at": pinned_at_iso, "pin_weight": 1.0, "ttl": 1000}]
        out = architect.apply_pin_boost(cands, now=now)
        assert out[0]["boosted_score"] > 1.0
        assert out[0]["boosted_score"] < 2.0  # decay < 1


# ---------------------------------------------------------------------------
# hits.jsonl logger
# ---------------------------------------------------------------------------

class TestHitsLogger:
    def test_log_hit_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hits.jsonl"
            rec = architect.log_hit({"query": "hello", "score": 0.9}, hits_file=p, pin_weight=1.0, ttl=3600)
            assert p.exists()
            assert rec["query"] == "hello"
            assert rec["pin_weight"] == 1.0
            assert "boosted_score" in rec
            # read back
            hits = architect.read_hits(p)
            assert len(hits) == 1
            assert hits[0]["query"] == "hello"

    def test_log_hit_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hits.jsonl"
            rec = architect.log_hit({"query": "hi"}, hits_file=p, dry_run=True)
            assert rec["_dry_run"] is True
            assert not p.exists()

    def test_log_hit_appends(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hits.jsonl"
            architect.log_hit({"id": 1}, hits_file=p)
            architect.log_hit({"id": 2}, hits_file=p)
            hits = architect.read_hits(p)
            assert len(hits) == 2
            assert hits[0]["id"] == 1
            assert hits[1]["id"] == 2

    def test_read_hits_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nonexistent.jsonl"
            assert architect.read_hits(p) == []

    def test_read_hits_limit(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hits.jsonl"
            for i in range(5):
                architect.log_hit({"id": i}, hits_file=p)
            hits = architect.read_hits(p, limit=2)
            assert len(hits) == 2
            assert hits[0]["id"] == 3
            assert hits[1]["id"] == 4


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

class TestWebhook:
    def test_no_url_returns_not_notified(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_WEBHOOK_URL", None)
            res = architect.notify_webhook({"event": "test"})
            assert res["notified"] is False
            assert "no webhook" in res["reason"].lower()

    def test_dry_run_no_http(self):
        with patch.dict(os.environ, {"HERMES_WEBHOOK_URL": "https://example.com/hook"}):
            res = architect.notify_webhook({"event": "test"}, dry_run=True)
            assert res["dry_run"] is True
            assert res["notified"] is False

    def test_explicit_url_dry_run(self):
        res = architect.notify_webhook({"event": "test"}, url="https://example.com/hook", dry_run=True)
        assert res["dry_run"] is True
        assert res["url"] == "https://example.com/hook"


# ---------------------------------------------------------------------------
# gh PR creation (dry-run paths)
# ---------------------------------------------------------------------------

class TestGhPr:
    def test_create_pr_dry_run(self):
        res = architect.create_pr("feat/x", "title", "body", dry_run=True)
        assert res["dry_run"] is True
        assert res["created"] is False

    def test_git_push_dry_run(self):
        res = architect.git_push_branch("feat/x", dry_run=True)
        assert res["dry_run"] is True
        assert res["pushed"] is False


# ---------------------------------------------------------------------------
# Audit helpers (V3 compat — mock conn)
# ---------------------------------------------------------------------------

class TestAudit:
    def test_audit_decision_dry_run(self):
        conn = MagicMock()
        res = architect.audit_decision(conn, "decide X", "because Y", {"k": "v"}, dry_run=True)
        assert res["written"] is False
        assert res["dry_run"] is True
        conn.cursor.assert_not_called()

    def test_audit_decision_writes(self):
        cur = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = cur
        cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cm
        res = architect.audit_decision(conn, "decide X", dry_run=False)
        assert res["written"] is True
        cur.execute.assert_called_once()

    def test_audit_schema_change_dry_run(self):
        conn = MagicMock()
        res = architect.audit_schema_change(conn, "V4", "V4__foo.sql", "abc", dry_run=True)
        assert res["written"] is False

    def test_audit_schema_change_writes(self):
        cur = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = cur
        cm.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cm
        res = architect.audit_schema_change(conn, "V4", "V4__foo.sql", "abc", dry_run=False)
        assert res["written"] is True


# ---------------------------------------------------------------------------
# CODEOWNERS + .env.example existence checks
# ---------------------------------------------------------------------------

class TestRepoArtifacts:
    def test_codeowners_exists_and_covers_paths(self):
        p = REPO_ROOT / ".github" / "CODEOWNERS"
        assert p.exists(), ".github/CODEOWNERS missing"
        text = p.read_text()
        assert "scripts/migrate.py" in text
        assert "scripts/architect.py" in text
        assert ".github/workflows/" in text

    def test_env_example_documents_keys(self):
        p = REPO_ROOT / ".env.example"
        assert p.exists()
        text = p.read_text()
        for key in ("HERMES_AGENT_ENABLED", "HERMES_GITHUB_TOKEN", "HERMES_WEBHOOK_URL", "HERMES_PIN_WEIGHT", "HERMES_PIN_TTL_SECONDS", "pin_weight", "ttl"):
            assert key in text, f"{key} not documented in .env.example"


# ---------------------------------------------------------------------------
# Dry-run integration
# ---------------------------------------------------------------------------

class TestDryRunIntegration:
    def test_main_dry_run_with_enabled(self, capsys):
        with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "true"}):
            rc = architect.main(["--dry-run", "--branch", "feat/test", "--title", "Test PR"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "DRY-RUN" in out

    def test_generate_migration_via_cli(self):
        with tempfile.TemporaryDirectory() as td:
            sql_file = Path(td) / "input.sql"
            sql_file.write_text("CREATE TABLE t (id int);")
            migrations_dir = Path(td) / "migrations"
            with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "true"}):
                rc = architect.main([
                    "--migrations-dir", str(migrations_dir),
                    "--generate-migration", "V4", "test migration", str(sql_file),
                ])
                assert rc == 0
                assert (migrations_dir / "V4__test_migration.sql").exists()

            # dry-run should not write
            migrations_dir2 = Path(td) / "migrations2"
            with patch.dict(os.environ, {"HERMES_AGENT_ENABLED": "true"}):
                rc = architect.main([
                    "--dry-run",
                    "--migrations-dir", str(migrations_dir2),
                    "--generate-migration", "V5", "dry test", str(sql_file),
                ])
                assert rc == 0
                assert not (migrations_dir2 / "V5__dry_test.sql").exists()
