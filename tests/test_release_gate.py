"""The release gate must not inherit authority over an operator's installation."""

import importlib.util
import io
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_gate.py"


def load_gate():
    assert SCRIPT.is_file(), "disposable release gate is not implemented"
    spec = importlib.util.spec_from_file_location("release_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_wheel(path, *, provider="hermes_memory:register", scripts=True):
    path.mkdir(parents=True, exist_ok=True)
    wheel = path / "hermes_memory-0.1.0-py3-none-any.whl"
    entries = "[hermes_agent.memory_providers]\nhybrid-age = " + provider + "\n"
    if scripts:
        entries += "[console_scripts]\n"
        for name in ("install", "uninstall", "verify"):
            entries += f"hermes-memory-{name} = hermes_memory.{name}:main\n"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_memory-0.1.0.dist-info/entry_points.txt", entries)
        archive.writestr("hermes_memory/__init__.py", "def register(ctx): pass\n")
    return wheel


def test_default_runs_disposable_checks_without_operator_credentials(tmp_path, monkeypatch):
    gate = load_gate()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='example'\n")
    (repo / ".env").write_text("DO_NOT_COPY=secret\n")
    (repo / "src").mkdir()
    (repo / "src" / ".env.production").write_text("DO_NOT_COPY=secret\n")
    (repo / "src" / "linked.py").symlink_to(repo / ".env")
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "real-hermes"))
    monkeypatch.setenv("HYBRID_AGE_DSN", "postgres://operator:secret@localhost:5450/hermes_memory")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--migrate")
    calls = []
    homes = []

    def runner(argv, *, cwd, env):
        calls.append(argv)
        homes.append(Path(env["HOME"]))
        assert Path(env["HOME"]).is_dir()
        assert Path(env["HERMES_HOME"]).is_relative_to(env["HOME"])
        assert env["HOME"] != str(tmp_path / "real-home")
        if "provider-registration" in argv[-1]:
            # Only the construction smoke receives a non-listening placeholder, never the operator DSN.
            assert env["HYBRID_AGE_DSN"] == "postgresql://127.0.0.1:1/hermes_release_gate_smoke"
        else:
            assert "HYBRID_AGE_DSN" not in env
        assert "PYTEST_ADDOPTS" not in env
        assert env["PIP_NO_INDEX"] == "1"
        assert cwd != repo
        assert not (cwd / ".env").exists()
        assert not (cwd / "src" / ".env.production").exists()
        assert not (cwd / "src" / "linked.py").exists()
        if "wheel" in argv:
            write_wheel(Path(argv[argv.index("--wheel-dir") + 1]))
        return subprocess.CompletedProcess(argv, 0, "do-not-print-secret", "")

    output = io.StringIO()
    assert gate.run_static(repo, runner=runner, output=output) == 0
    assert all(not home.exists() for home in homes)
    assert any("git" == cmd[0] and "init" in cmd for cmd in calls)
    assert any("git" == cmd[0] and "commit" in cmd for cmd in calls)
    assert any("pytest" in cmd for cmd in calls)
    assert any("ruff" in cmd and cmd[-3:] == ["src", "tests", "scripts/release_gate.py"] for cmd in calls)
    assert any("mypy" in cmd for cmd in calls)
    assert any("wheel" in cmd and "--no-build-isolation" in cmd for cmd in calls)
    assert any("provider-registration" in cmd[-1] for cmd in calls)
    assert not any(set(cmd) & {"docker", "dropdb", "hermes-memory-install", "hermes-memory-uninstall"} for cmd in calls)
    assert "do-not-print-secret" not in output.getvalue()
    assert "PASS" in output.getvalue()


def test_static_stops_at_failed_command_and_suppresses_sensitive_output(tmp_path):
    gate = load_gate()
    (tmp_path / "pyproject.toml").write_text("")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 17, "password=secret", "postgres://u:p@h/db")

    output = io.StringIO()
    assert gate.run_static(tmp_path, runner=runner, output=output) == 17
    assert len(calls) == 1
    assert "secret" not in output.getvalue()
    assert "postgres://" not in output.getvalue()
    assert "FAIL" in output.getvalue()


@pytest.mark.parametrize("dsn", [
    "", "postgres://u:p@localhost:5450/hermes_release_gate_x",
    "postgres://u:p@localhost:5452/hermes_release_gate_x",
    "postgres://u:p@localhost:5549/hermes_memory",
    "postgres://u:p@localhost:5549/hermes_memory_installed",
    "postgres://u:p@localhost:5549/hermes_test",
    "postgres://u:p@remote.example:5549/hermes_release_gate_x",
    "postgres://u:p@localhost/hermes_release_gate_x",
    "postgres://u:p@localhost:5549/hermes_release_gate_x?host=remote.example",
    "postgres://u:p@localhost:5549/hermes_release_gate_x#fragment",
    "postgres://u:p@localhost:5549/hermes_release_gate_",
    "postgres://u:p@localhost:5549/ordinary_database",
    "host=localhost port=5549 dbname=hermes_release_gate_x password=secret",
    "postgres://u:p@localhost:bad/hermes_release_gate_x",
    "postgres://u:p@localhost:0/hermes_release_gate_x",
])
def test_live_rejects_targets_that_are_not_explicitly_isolated(dsn):
    gate = load_gate()
    with pytest.raises(ValueError, match="isolated"):
        gate.validate_live_dsn(dsn)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_live_plan_is_guarded_redacted_and_never_executes(host, monkeypatch):
    gate = load_gate()
    dsn = f"postgresql://user:very-secret@{host}:5549/hermes_release_gate_case1"
    monkeypatch.setenv("HOME", "/operator/home")
    output = io.StringIO()
    assert gate.main(["--live", "--dsn", dsn], output=output) == 2
    text = output.getvalue()
    assert "very-secret" not in text
    assert dsn not in text
    assert "BLOCKED" in text
    assert "restart" in text and "recall" in text and "reinstall" in text
    assert "/operator/home" not in text
    assert "level-6" in text


def test_cli_rejects_live_without_dsn_and_dsn_without_live():
    gate = load_gate()
    for args in (["--live"], ["--dsn", "postgres://u:secret@localhost:5549/hermes_release_gate_x"]):
        output = io.StringIO()
        assert gate.main(args, output=output) == 2
        assert "secret" not in output.getvalue()


def test_wheel_validation_rejects_missing_or_wrong_discovery_contract(tmp_path):
    gate = load_gate()
    wheel = write_wheel(tmp_path, provider="wrong:entry")
    with pytest.raises(ValueError, match="provider"):
        gate.inspect_wheel(wheel)
    wheel = write_wheel(tmp_path, scripts=False)
    with pytest.raises(ValueError, match="console"):
        gate.inspect_wheel(wheel)
    wheel = write_wheel(tmp_path)
    gate.inspect_wheel(wheel)


def test_wheel_smoke_registers_without_real_configuration(monkeypatch):
    gate = load_gate()
    monkeypatch.delenv("HYBRID_AGE_DSN", raising=False)

    def build_and_smoke_only(argv, *, cwd, env):
        # Exercise the actual artifact in a fresh interpreter without recursively
        # running pytest. The rest of the command plan is covered above.
        if "wheel" in argv or "provider-registration" in argv[-1]:
            return gate.subprocess_runner(argv, cwd=cwd, env=env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    output = io.StringIO()
    assert gate.run_static(SCRIPT.parents[1], runner=build_and_smoke_only, output=output) == 0, output.getvalue()


def test_execute_requires_isolated_embed_url_and_refuses_operator_ollama():
    gate = load_gate()
    dsn = "postgresql://user:secret@127.0.0.1:5549/hermes_release_gate_case2"
    output = io.StringIO()
    assert gate.main(["--live", "--dsn", dsn, "--execute"], output=output) == 2
    assert "secret" not in output.getvalue()
    with pytest.raises(ValueError, match="isolated"):
        gate.validate_embed_url("http://127.0.0.1:11434/v1")
    with pytest.raises(ValueError, match="isolated"):
        gate.validate_embed_url("http://ollama.example:11436/v1")
    gate.validate_embed_url("http://127.0.0.1:11436/v1")


def test_live_run_stops_on_failed_snapshot_and_never_targets_operator_services(tmp_path, monkeypatch):
    gate = load_gate()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='example'\n")
    monkeypatch.setattr(gate.shutil, "which", lambda name: "/usr/bin/hermes" if name == "hermes" else None)
    calls = []

    def runner(argv, *, cwd, env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    dsn = "postgresql://hermes:secret@127.0.0.1:5461/hermes_release_gate_ab12"
    output = io.StringIO()
    assert gate.live_run(dsn, "http://127.0.0.1:11436/v1", repo=repo, runner=runner, output=output) == 1
    # Only the failed Git step and read-only Docker inspection (cleanup probes) may run.
    assert calls[0][0] == "git"
    assert all(call[:2] in (["docker", "container"], ["docker", "volume"]) and "inspect" in call for call in calls[1:])
    flat = " ".join(" ".join(argv) for argv in calls)
    for operator in ("5450", "5452", "11434", "hermes-memory-installed", "hermes_memory_installed", "/.hermes/plugins"):
        assert operator not in flat
    assert "secret" not in output.getvalue()


def test_live_run_refuses_without_hermes_cli(tmp_path, monkeypatch):
    gate = load_gate()
    monkeypatch.setattr(gate.shutil, "which", lambda name: None)
    dsn = "postgresql://hermes:secret@127.0.0.1:5461/hermes_release_gate_ab12"
    output = io.StringIO()
    assert gate.live_run(dsn, "http://127.0.0.1:11436/v1", repo=tmp_path, runner=None, output=output) == 1
    assert "hermes CLI not found" in output.getvalue()


def test_derived_compose_names_match_installer_contract():
    gate = load_gate()
    assert gate.derived_compose_names("postgresql://u:p@127.0.0.1:5461/hermes_release_gate_ab12") == (
        "hermes-release-gate-ab12", "hermes-release-gate-ab12-postgres", "hermes-release-gate-ab12_pgdata")
