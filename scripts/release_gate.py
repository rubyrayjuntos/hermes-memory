#!/usr/bin/env python3
"""Disposable release gate.

Default: offline static checks in a disposable snapshot (no lifecycle commands).
--live --dsn ... --embed-url ...: prints a plan only (exit 2).
--live --dsn ... --embed-url ... --execute: runs the install/verify/uninstall/
reinstall lifecycle in a temporary venv and temporary HOME against a derived,
release-gate-only Docker database. The operator's Hermes home, :5450 dev stack,
:5452 installed stack and :11434 Ollama are never targeted.

Child stdout/stderr are withheld unless --log-dir is given (operator-chosen,
local, may contain disposable-test secrets).
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TextIO
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile


class GateError(Exception):
    """A safe-to-display gate failure."""


OPERATOR_PORTS = {5450, 5452}
OPERATOR_EMBED_PORT = 11434


def validate_live_dsn(dsn: str) -> None:
    """Accept only explicit single-host URI targets with release-gate names.

    Queries/fragments are forbidden: libpq options can override the URL host,
    database or port, or read credentials from a caller's service/pass file.
    """
    message = "Live mode requires an isolated loopback DSN with an explicit safe port and release-gate database"
    try:
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.removeprefix("/"))
        valid = (
            parsed.scheme in {"postgres", "postgresql"}
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port is not None
            and 0 < parsed.port <= 65535
            and parsed.port not in OPERATOR_PORTS
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() for c in dsn)
            and re.fullmatch(r"hermes_release_gate_[a-z0-9_]+", database)
            and len(database) <= 63
        )
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)


def validate_embed_url(url: str) -> None:
    """The embedding endpoint must be an isolated loopback Ollama, not the operator's."""
    message = "Live mode requires an isolated loopback embed URL on a port other than the operator's Ollama (11434)"
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port is not None
            and parsed.port != OPERATOR_EMBED_PORT
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() for c in url)
        )
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)


def derived_compose_names(dsn: str) -> tuple[str, str, str]:
    """Mirror of install_cli.release_gate_target: (project, container, volume)."""
    database = unquote(urlsplit(dsn).path.removeprefix("/"))
    project = "hermes-release-gate-" + database.removeprefix("hermes_release_gate_").replace("_", "-")
    return project, f"{project}-postgres", f"{project}_pgdata"


REQUIRED_WHEEL_MEMBERS = (
    "hermes_memory/_bundle/build_info.json",
    "hermes_memory/_bundle/docker/Dockerfile",
    "hermes_memory/_bundle/sql/migrations/V1__vector_age.sql",
    "hermes_memory/_bundle/scripts/migrate.py",
    "hermes_memory/_bundle/skills/librarian-setup/SKILL.md",
)


def inspect_wheel(path: Path) -> None:
    """Fail if the artifact loses provider discovery or lifecycle commands."""
    with ZipFile(path) as archive:
        metadata = [n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
        if len(metadata) != 1:
            raise ValueError("wheel requires one provider entry-point metadata file")
        config = configparser.ConfigParser()
        config.read_string(archive.read(metadata[0]).decode())
        if dict(config.items("hermes_agent.memory_providers")) != {"hybrid-age": "hermes_memory:register"}:
            raise ValueError("wheel provider discovery contract is invalid")
        expected = {f"hermes-memory-{name}" for name in ("install", "uninstall", "verify")}
        if not config.has_section("console_scripts") or not expected <= set(config["console_scripts"]):
            raise ValueError("wheel console lifecycle entry points are missing")
        if "hermes_memory/__init__.py" not in archive.namelist():
            raise ValueError("wheel provider package is missing")
        missing = [name for name in REQUIRED_WHEEL_MEMBERS if name not in archive.namelist()]
        if missing:
            # Without these the installer cannot run outside a Git checkout.
            raise ValueError("wheel lacks bundled install assets")


def subprocess_runner(argv: list[str], *, cwd: Path, env: dict[str, str]):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _stage_source(repo: Path, destination: Path) -> None:
    # Do not copy .env, arbitrary operator files, Git internals or linked paths.
    allowed = {"src", "tests", "scripts", "sql", "docs", ".github", "skills", "docker"}
    allowed |= {"pyproject.toml", "setup.py", "hermes_build_info.py", "README.md", "LICENSE", ".env.example"}
    ignored = shutil.ignore_patterns("__pycache__", "*.egg-info", ".env", ".env.local", ".venv")

    def ignore(directory, names):
        return set(ignored(directory, names)) | {
            name for name in names if (Path(directory) / name).is_symlink()
            or (name.startswith(".env") and name != ".env.example")
        }

    destination.mkdir()
    for name in sorted(allowed):
        source = repo / name
        if source.is_symlink():
            continue
        if source.is_dir():
            shutil.copytree(source, destination / name, ignore=ignore)
        elif source.is_file():
            shutil.copy2(source, destination / name)


def _git_snapshot_commands() -> list[tuple[str, list[str]]]:
    return [
        ("disposable Git init", ["git", "-c", "init.templateDir=", "init", "-b", "release-gate"]),
        ("disposable Git snapshot", ["git", "add", "."]),
        ("disposable Git commit", ["git", "-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "Disposable release gate snapshot"]),
    ]


def run_static(repo: Path, *, runner=subprocess_runner, output: TextIO = sys.stdout) -> int:
    """Execute static checks through an injectable runner, never lifecycle tools."""
    with tempfile.TemporaryDirectory(prefix="hermes-release-gate-") as temporary:
        root = Path(temporary)
        home = root / "home"
        hermes_home = home / ".hermes"
        hermes_home.mkdir(parents=True)
        source = root / "source"
        _stage_source(repo, source)
        env = {
            "PATH": os.defpath, "HOME": str(home), "HERMES_HOME": str(hermes_home),
            "LANG": "C.UTF-8", "PYTHONPATH": str(source / "src"),
            "PYTHONNOUSERSITE": "1", "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CONFIG_FILE": os.devnull,
            "XDG_CACHE_HOME": str(home / ".cache"), "TMPDIR": str(root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        }
        python = sys.executable
        dist = root / "dist"
        commands = _git_snapshot_commands() + [
            ("pytest (non-integration; no migrations)", [python, "-m", "pytest", "-q"]),
            ("ruff (CI release scope)", [python, "-m", "ruff", "check", "src", "tests", "scripts/release_gate.py"]),
            ("mypy (declared scope)", [python, "-m", "mypy"]),
            ("wheel (offline)", [python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(dist), "."]),
        ]
        print("Static gate: disposable source, HOME and Hermes home; child output withheld for privacy.", file=output)

        def run(label, argv, env_override=None):
            print(f"RUN {label}", file=output, flush=True)
            try:
                result = runner(argv, cwd=source, env=env_override or env)
            except OSError:
                raise GateError("command could not start") from None
            print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {label}: exit {result.returncode}", file=output, flush=True)
            return result.returncode

        try:
            for label, argv in commands:
                status = run(label, argv)
                if status:
                    return status
            wheels = list(dist.glob("*.whl"))
            if len(wheels) != 1:
                raise GateError("expected exactly one built wheel")
            inspect_wheel(wheels[0])
            print("PASS wheel entry-point metadata", file=output)
            # A fresh interpreter imports from the artifact, not the source tree.
            smoke = (
                "import sys; sys.path.insert(0, " + repr(str(wheels[0])) + "); "
                "import hermes_memory; "
                "assert '.whl/' in hermes_memory.__file__; "
                "providers=[]; "
                "ctx=type('Context', (), {'register_memory_provider': lambda self, p: providers.append(p)})(); "
                "hermes_memory.register(ctx); "
                "assert len(providers)==1; "
                "assert type(providers[0]).__name__=='HybridAgeMemoryProvider' "
                "# provider-registration"
            )
            # Construction only loads configuration (no connection). Supply a
            # non-listening placeholder so the fail-closed DSN check is satisfied
            # without any real target.
            smoke_env = {**env, "HYBRID_AGE_DSN": "postgresql://127.0.0.1:1/hermes_release_gate_smoke"}
            status = run("wheel provider registration", [python, "-c", smoke], env_override=smoke_env)
            if status:
                return status
        except (GateError, ValueError, configparser.Error, OSError):
            print("FAIL gate artifact/check setup (details withheld for privacy)", file=output)
            return 1
        print("PASS static release checks; no live lifecycle or level-6 evidence.", file=output)
        return 0


class _Checks:
    """Printed, recorded boolean assertions over the disposable run."""

    def __init__(self, output: TextIO) -> None:
        self.output = output
        self.failed: list[str] = []

    def __call__(self, label: str, ok: bool) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} check: {label}", file=self.output, flush=True)
        if not ok:
            self.failed.append(label)
        return ok


def _live_env(root: Path, hermes_bin_dir: Path) -> dict[str, str]:
    home = root / "home"
    env = {
        "PATH": os.pathsep.join([str(hermes_bin_dir), os.defpath]),
        "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
        "LANG": "C.UTF-8", "TMPDIR": str(root), "PYTHONNOUSERSITE": "1",
        "XDG_CACHE_HOME": str(home / ".cache"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for key in ("DOCKER_HOST", "XDG_RUNTIME_DIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _step(label, argv, *, cwd, env, runner, output, log_dir):
    print(f"RUN {label}", file=output, flush=True)
    try:
        result = runner(argv, cwd=cwd, env=env)
    except OSError:
        print(f"FAIL {label}: command could not start", file=output, flush=True)
        return None
    if log_dir is not None:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        (log_dir / f"{slug}.log").write_text(f"{result.stdout}\n--- stderr ---\n{result.stderr}\n", encoding="utf-8")
    print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {label}: exit {result.returncode}", file=output, flush=True)
    return result


def live_plan(dsn: str, embed_url: str | None, *, output: TextIO) -> int:
    validate_live_dsn(dsn)
    print("BLOCKED live plan only: isolated DSN accepted [REDACTED]; generated temporary HOME/Hermes home.", file=output)
    print("No commands executed. Re-run with --execute to run the lifecycle. No level-6 evidence.", file=output)
    for step in (
        "Stage a git snapshot of the reviewed tree; create a temporary venv.",
        "install: venv python -m hermes_memory.install_cli --ref HEAD --release-gate-target --dsn [REDACTED] --embed-url <isolated-loopback>",
        "verify: venv python -m hermes_memory.verify --dsn [REDACTED] --require-embed",
        "hermes memory status (temporary HOME) must report hybrid-age.",
        "uninstall --keep-db: plugin/config/env ownership removed; database volume retained.",
        "reinstall and verify; hermes memory status again.",
        "uninstall --force: all run-owned plugin, config, env, container and volume removed.",
    ):
        print(f"PLANNED (not executed): {step}", file=output)
    print("NOT COVERED: Hermes recall of seeded facts across restart (needs a configured chat model) "
          "and owned-service restart; the gate asserts provider status, not recall.", file=output)
    print(f"Embed URL for --execute must be isolated loopback, not port {OPERATOR_EMBED_PORT}; "
          "provisioning the isolated Ollama and Postgres image is an operator step.", file=output)
    return 2


def live_run(dsn: str, embed_url: str, *, repo: Path, runner=subprocess_runner,
             output: TextIO = sys.stdout, log_dir: Path | None = None) -> int:
    validate_live_dsn(dsn)
    validate_embed_url(embed_url)
    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        print("FAIL live preflight: hermes CLI not found on PATH", file=output)
        return 1
    project, container, volume = derived_compose_names(dsn)
    password = urlsplit(dsn).password or ""
    checks = _Checks(output)
    print("Live lifecycle: temporary HOME and venv; release-gate-only database; operator services untouched.", file=output)

    with tempfile.TemporaryDirectory(prefix="hermes-release-gate-live-") as tmp:
        root = Path(tmp)
        home = root / "home"
        (home / ".hermes").mkdir(parents=True)
        hermes_home = home / ".hermes"
        env = _live_env(root, Path(hermes_bin).parent)
        source = root / "source"
        venv_python = root / "venv" / "bin" / "python"
        plugin_dir = hermes_home / "plugins" / "hybrid-age"
        state_file = hermes_home / "hermes-memory-install.json"
        env_file = hermes_home / ".env"
        config_file = hermes_home / "config.yaml"

        def step(label, argv, *, cwd=root, extra=None):
            return _step(label, argv, cwd=cwd, env={**env, **(extra or {})}, runner=runner,
                         output=output, log_dir=log_dir)

        def container_exists() -> bool:
            result = runner(["docker", "container", "inspect", container], cwd=root, env=env)
            return result.returncode == 0

        def volume_exists() -> bool:
            result = runner(["docker", "volume", "inspect", volume], cwd=root, env=env)
            return result.returncode == 0

        def installed_checks(prefix: str) -> None:
            checks(f"{prefix}: plugin directory present", plugin_dir.is_dir())
            checks(f"{prefix}: ownership record present", state_file.is_file())
            checks(f"{prefix}: config records hybrid_age block", config_file.is_file() and "hybrid_age" in config_file.read_text())
            if state_file.is_file() and password:
                checks(f"{prefix}: ownership record holds no password", password not in state_file.read_text())

        def removed_checks(prefix: str, *, keep_db: bool) -> None:
            checks(f"{prefix}: plugin directory removed", not plugin_dir.exists())
            checks(f"{prefix}: ownership record removed", not state_file.exists())
            if env_file.is_file():
                text = env_file.read_text()
                checks(f"{prefix}: owned environment values removed", "HYBRID_AGE_DSN" not in text and (not password or password not in text))
            if config_file.is_file():
                checks(f"{prefix}: hybrid_age config block removed", "hybrid_age" not in config_file.read_text())
            checks(f"{prefix}: database container removed", not container_exists())
            checks(f"{prefix}: database volume {'retained' if keep_db else 'removed'}",
                   volume_exists() if keep_db else not volume_exists())

        installer = [str(venv_python), "-m", "hermes_memory.install_cli", "--ref", "HEAD",
                     "--dsn", dsn, "--embed-url", embed_url, "--embed-model", "nomic-embed-text",
                     "--release-gate-target", "--yes"]
        verify = [str(venv_python), "-m", "hermes_memory.verify", "--dsn", dsn, "--require-embed"]
        uninstall_keep_db = [str(venv_python), "-m", "hermes_memory.uninstall_cli", "--force", "--keep-db", "--dsn", dsn]
        uninstall_full = [str(venv_python), "-m", "hermes_memory.uninstall_cli", "--force", "--dsn", dsn]
        hermes_status = [hermes_bin, "memory", "status"]

        try:
            _stage_source(repo, source)
            for label, argv in _git_snapshot_commands():
                result = step(label, argv, cwd=source)
                if result is None or result.returncode != 0:
                    print("FAIL snapshot: a disposable Git step failed; lifecycle not started", file=output)
                    return 1
            result = step("disposable venv", [sys.executable, "-m", "venv", str(root / "venv")])
            if result is None or result.returncode != 0:
                print("FAIL venv: could not create the disposable interpreter", file=output)
                return 1

            result = step("install (pinned HEAD, release-gate target)", installer, cwd=source,
                          extra={"PYTHONPATH": str(source / "src")})
            checks("install exit 0", result is not None and result.returncode == 0)
            installed_checks("after install")

            result = step("verify (installed package, --require-embed)", verify)
            checks("verify exit 0", result is not None and result.returncode == 0)

            result = step("hermes memory status", hermes_status)
            checks("hermes reports hybrid-age as the memory provider",
                   result is not None and result.returncode == 0 and "hybrid-age" in result.stdout)

            result = step("uninstall --keep-db", uninstall_keep_db)
            checks("uninstall --keep-db exit 0", result is not None and result.returncode == 0)
            removed_checks("after uninstall --keep-db", keep_db=True)

            result = step("reinstall (pinned HEAD, release-gate target)", installer, cwd=source,
                          extra={"PYTHONPATH": str(source / "src")})
            checks("reinstall exit 0", result is not None and result.returncode == 0)
            installed_checks("after reinstall")

            result = step("verify after reinstall", verify)
            checks("verify after reinstall exit 0", result is not None and result.returncode == 0)

            result = step("hermes memory status after reinstall", hermes_status)
            checks("hermes reports hybrid-age after reinstall",
                   result is not None and result.returncode == 0 and "hybrid-age" in result.stdout)

            result = step("uninstall --force (full)", uninstall_full)
            checks("full uninstall exit 0", result is not None and result.returncode == 0)
            removed_checks("after full uninstall", keep_db=False)
        finally:
            # Run-owned cleanup only: Hermes state via the uninstaller, then the exact
            # container/volume names derived from the validated release-gate DSN.
            if state_file.exists():
                step("cleanup: uninstall --force (after failure)", uninstall_full)
            if container_exists():
                step("cleanup: remove run-owned container", ["docker", "rm", "-f", container])
            if volume_exists():
                step("cleanup: remove run-owned volume", ["docker", "volume", "rm", volume])

    if checks.failed:
        print(f"FAIL live lifecycle: {len(checks.failed)} check(s) failed.", file=output)
        return 1
    print("PASS live lifecycle in disposable home; human-observed level-6 evidence still required.", file=output)
    return 0


def main(argv: list[str] | None = None, *, output: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="live lifecycle (plan unless --execute)")
    parser.add_argument("--dsn", help="explicit isolated URI; never echoed")
    parser.add_argument("--embed-url", help="isolated loopback Ollama /v1 URL (not port 11434)")
    parser.add_argument("--execute", action="store_true", help="run the live lifecycle (requires --live and --embed-url)")
    parser.add_argument("--log-dir", type=Path, help="write child transcripts here (operator-chosen; may contain test secrets)")
    args = parser.parse_args(argv)
    if bool(args.dsn) != args.live:
        print("FAIL --live requires --dsn, and --dsn requires --live", file=output)
        return 2
    if args.execute and not (args.live and args.embed_url):
        print("FAIL --execute requires --live and --embed-url", file=output)
        return 2
    if args.live:
        try:
            if not args.execute:
                return live_plan(args.dsn, args.embed_url, output=output)
            if args.log_dir is not None:
                args.log_dir.mkdir(parents=True, exist_ok=True)
            return live_run(args.dsn, args.embed_url, repo=Path(__file__).resolve().parents[1],
                            output=output, log_dir=args.log_dir)
        except ValueError:
            print("FAIL live target is not explicitly isolated; DSN withheld", file=output)
            return 2
    return run_static(Path(__file__).resolve().parents[1], output=output)


if __name__ == "__main__":
    raise SystemExit(main())
