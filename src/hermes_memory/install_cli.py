#!/usr/bin/env python3
"""hermes-memory-install — First-time installation CLI.

Installs a *pinned git tree*, not the working clone:

- Plugin files come from ``git archive <ref>`` (committed snapshot).
- ``pip install`` is non-editable from that archive (not ``pip install -e .``).
- ``~/.hermes/plugins/hybrid-age/.hermes-memory-version`` records the SHA.
- Default database is ``hermes_memory_installed`` on ``127.0.0.1:5452``,
  compose project ``hermes-memory-installed`` — not the dev stack on ``:5450``.
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_HOME = Path.home() / ".hermes"

PLUGIN_DIRS = (
    HERMES_HOME / "plugins" / "hybrid-age",
    HERMES_HOME / "profiles" / "librarian" / "plugins" / "hybrid-age",
)
CONFIG_PATHS = (
    HERMES_HOME / "config.yaml",
    HERMES_HOME / "profiles" / "librarian" / "config.yaml",
)
VERSION_FILENAME = ".hermes-memory-version"
INSTALLED_COMPOSE_PROJECT = "hermes-memory-installed"
INSTALLED_CONTAINER = "hermes-memory-postgres-installed"
INSTALLED_PORT = "5452"
INSTALLED_DB = "hermes_memory_installed"
DEV_PORT = 5450
DEV_DB = "hermes_memory"


def default_release_ref(repo: Path) -> str:
    """GitHub-tracking ref, not a working tree and not the stale v0.1.0 tag.

    Prefer ``origin/main`` so an install follows what GitHub has, not local
    dirty files. ``v0.1.0`` predates ``beam_score`` and must never be implicit.
    """
    for candidate in ("origin/main", "main"):
        check = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{candidate}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            return candidate
    return "HEAD"


def resolve_pin(repo: Path, ref: str) -> tuple[str, str]:
    """Return ``(ref, full_sha)`` from git. Does not read the working tree."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"install: cannot resolve --ref {ref!r}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return ref, result.stdout.strip()


def export_pin(repo: Path, sha: str, dest: Path) -> None:
    """Extract the committed tree at ``sha`` into ``dest`` via git archive."""
    dest.mkdir(parents=True, exist_ok=True)
    archived = subprocess.run(
        ["git", "-C", str(repo), "archive", sha],
        capture_output=True,
        check=False,
    )
    if archived.returncode != 0:
        err = archived.stderr.decode("utf-8", errors="replace")
        raise SystemExit(f"install: git archive {sha} failed: {err.strip()}")
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:
            tar.extractall(dest)


def write_version_stamp(plugin_dir: Path, *, sha: str, ref: str) -> Path:
    stamp = plugin_dir / VERSION_FILENAME
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp.write_text(f"sha={sha}\nref={ref}\ninstalled_at={now}\n", encoding="utf-8")
    return stamp


def read_version_stamp(plugin_dir: Path) -> dict[str, str]:
    stamp = plugin_dir / VERSION_FILENAME
    if not stamp.is_file():
        return {}
    out: dict[str, str] = {}
    for line in stamp.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def copy_plugin_tree(src_pkg: Path, plugin_dir: Path) -> None:
    """Copy a pinned package tree. Never symlink to a clone."""
    if not src_pkg.is_dir():
        raise SystemExit(f"install: pinned tree missing {src_pkg}")
    if plugin_dir.is_symlink() or plugin_dir.is_file():
        plugin_dir.unlink()
    elif plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src_pkg,
        plugin_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def is_dev_clone_dsn(dsn: str) -> bool:
    """True when DSN is the loopback dev stack (:5450 / hermes_memory)."""
    parsed = urlparse(dsn)
    if not parsed.scheme.startswith("postgres"):
        return False
    host = (parsed.hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return False
    port = parsed.port or 5432
    db = (parsed.path or "/").rsplit("/", 1)[-1]
    return port == DEV_PORT and db == DEV_DB


def installed_dsn(password: str, *, host: str = "127.0.0.1") -> str:
    return f"postgres://hermes:{password}@{host}:{INSTALLED_PORT}/{INSTALLED_DB}"


def write_installed_compose_override(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "services:\n"
        "  postgres:\n"
        f"    container_name: {INSTALLED_CONTAINER}\n"
        f'    ports:\n'
        f'      - "127.0.0.1:{INSTALLED_PORT}:5432"\n'
        "volumes:\n"
        "  pgdata:\n"
        f"    name: {INSTALLED_COMPOSE_PROJECT}_pgdata\n",
        encoding="utf-8",
    )


def write_hybrid_age_block(config_path: Path, embed_model: str, graph: str) -> None:
    new_block_dict = {
        "dsn_env": "HYBRID_AGE_DSN",
        "embed_url_env": "HYBRID_AGE_EMBED_URL",
        "embed_model": embed_model,
        "graph": graph,
        "vector_k": 12,
        "min_similarity": 0.55,
        "max_tokens": 1200,
    }
    block_text = (
        f"hybrid_age:\n  dsn_env: HYBRID_AGE_DSN\n  embed_url_env: HYBRID_AGE_EMBED_URL\n"
        f"  embed_model: {embed_model}\n  graph: {graph}\n  vector_k: 12\n"
        f"  min_similarity: 0.55\n  max_tokens: 1200\n"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml  # type: ignore

        if config_path.exists():
            cfg = yaml.safe_load(config_path.read_text()) or {}
            if not isinstance(cfg, dict):
                cfg = {}
        else:
            cfg = {}
        cfg["hybrid_age"] = new_block_dict
        if "memory" not in cfg or not isinstance(cfg.get("memory"), dict):
            cfg["memory"] = {}
        cfg["memory"]["provider"] = "hybrid-age"
        config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        return
    except Exception:
        pass
    if config_path.exists():
        config_text = config_path.read_text()
        if "hybrid_age:" in config_text:
            config_text = re.sub(
                r"^hybrid_age:.*?(?=^\w|\Z)",
                block_text,
                config_text,
                flags=re.DOTALL | re.MULTILINE,
            )
        else:
            config_text = config_text.rstrip() + "\n" + block_text
        config_path.write_text(config_text)
    else:
        config_path.write_text(block_text)


def get_env_files():
    return (HERMES_HOME / ".env", HERMES_HOME / "profiles" / "librarian" / ".env")


def merge_env_file(path: Path, updates: dict) -> None:
    """Read-modify-write: preserve existing keys, update only *updates*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    order: list[str] = []
    raw_lines: list[str] = []
    if path.exists():
        raw_lines = path.read_text().splitlines()
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            k = k.strip()
            if k not in existing:
                order.append(k)
            existing[k] = v.strip()
    for k, v in updates.items():
        if k not in existing:
            order.append(k)
        existing[k] = v
    out_lines: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            if "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    out_lines.append(f"{k}={existing[k]}")
                    seen.add(k)
                elif k in existing and k not in seen:
                    out_lines.append(f"{k}={existing[k]}")
                    seen.add(k)
                else:
                    if k not in seen:
                        out_lines.append(line)
                        seen.add(k)
            else:
                out_lines.append(line)
    for k in order:
        if k not in seen:
            out_lines.append(f"{k}={existing[k]}")
            seen.add(k)
    path.write_text("\n".join(out_lines) + "\n")


def _redact_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    host = parsed.hostname or "?"
    port = parsed.port or "?"
    db = (parsed.path or "/").rsplit("/", 1)[-1] or "?"
    return f"{host}:{port}/{db}"


def _existing_env_dsn() -> str | None:
    dsn = os.environ.get("HYBRID_AGE_DSN")
    if dsn:
        return dsn
    for env_path in get_env_files():
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith("HYBRID_AGE_DSN="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
        except OSError:
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-memory-install",
        description="Install a pinned hermes-memory tree into Hermes (not the working clone).",
    )
    parser.add_argument("--dsn", default=None, help="Postgres DSN (must not be the :5450/hermes_memory dev stack unless --reuse-dsn)")
    parser.add_argument("--embed-url", default=None, help="Ollama API URL")
    parser.add_argument("--embed-model", default=None, help="Embedding model")
    parser.add_argument("--graph", default=None, help="AGE graph name")
    parser.add_argument(
        "--ref",
        default=None,
        help="Git ref to install (tag, branch, or SHA). Default: origin/main "
        "(GitHub), never v0.1.0. Use --ref HEAD for the local commit.",
    )
    parser.add_argument(
        "--reuse-dsn",
        action="store_true",
        help="Allow an explicit --dsn that points at the :5450/hermes_memory dev stack.",
    )
    parser.add_argument("--yes", action="store_true", help="Assume yes to all prompts")
    args = parser.parse_args(argv)

    ref = args.ref or default_release_ref(REPO_ROOT)
    ref, sha = resolve_pin(REPO_ROOT, ref)
    print(f"install: pin ref={ref} sha={sha}")

    dsn = args.dsn
    if dsn and is_dev_clone_dsn(dsn) and not args.reuse_dsn:
        print(
            f"ERROR: refusing dev-stack DSN {_redact_dsn(dsn)}. "
            "Installed memory uses "
            f"127.0.0.1:{INSTALLED_PORT}/{INSTALLED_DB}. Pass --reuse-dsn to override.",
            file=sys.stderr,
        )
        return 2
    if not dsn:
        existing = _existing_env_dsn()
        if existing and not is_dev_clone_dsn(existing):
            dsn = existing
        elif existing and is_dev_clone_dsn(existing) and not args.reuse_dsn:
            print(
                f"install: ignoring existing HYBRID_AGE_DSN {_redact_dsn(existing)} "
                f"(dev stack). Creating {INSTALLED_PORT}/{INSTALLED_DB}."
            )

    if not dsn or "***" in (dsn or ""):
        import getpass

        pw = os.environ.get("HERMES_PG_PASSWORD", "").strip()
        if not pw and args.yes:
            pw = secrets.token_urlsafe(20)
        if not pw:
            pw = getpass.getpass(
                "HERMES_PG_PASSWORD for the installed database "
                f"({INSTALLED_PORT}/{INSTALLED_DB}): "
            ).strip()
        if not pw:
            print("ERROR: a Postgres password is required.", file=sys.stderr)
            return 1
        dsn = installed_dsn(pw)
        for env_path in get_env_files():
            merge_env_file(env_path, {"HERMES_PG_PASSWORD": pw})

    def _prompt(label, default, env_name):
        if args.yes:
            return os.environ.get(env_name) or default
        return input(f"{label}: ").strip() or default

    embed_url = args.embed_url or _prompt(
        "HYBRID_AGE_EMBED_URL", "http://localhost:11434/v1", "HYBRID_AGE_EMBED_URL"
    )
    embed_model = args.embed_model or _prompt(
        "HYBRID_AGE_EMBED_MODEL", "nomic-embed-text", "HYBRID_AGE_EMBED_MODEL"
    )
    graph = args.graph or _prompt("HYBRID_AGE_GRAPH", "hermes_knowledge", "HYBRID_AGE_GRAPH")

    updates = {
        "HYBRID_AGE_DSN": dsn,
        "HYBRID_AGE_EMBED_URL": embed_url,
        "HYBRID_AGE_EMBED_MODEL": embed_model,
        "HYBRID_AGE_GRAPH": graph,
    }
    for env_path in get_env_files():
        merge_env_file(env_path, updates)
    os.environ["HYBRID_AGE_DSN"] = dsn
    os.environ.setdefault("HYBRID_AGE_EMBED_URL", embed_url)
    os.environ.setdefault("HYBRID_AGE_EMBED_MODEL", embed_model)
    os.environ.setdefault("HYBRID_AGE_GRAPH", graph)

    with tempfile.TemporaryDirectory(prefix="hermes-memory-pin-") as tmp:
        export = Path(tmp)
        print(f"[1/7] Exporting pinned tree {sha[:12]}…")
        export_pin(REPO_ROOT, sha, export)
        src_pkg = export / "src" / "hermes_memory"
        skill_src = export / "skills" / "librarian-setup"

        print("[1b/7] Installing hybrid-age plugin from pin (copy, not symlink)…")
        for plugin_dir in PLUGIN_DIRS:
            copy_plugin_tree(src_pkg, plugin_dir)
            stamp = write_version_stamp(plugin_dir, sha=sha, ref=ref)
            print(f"    {plugin_dir} sha={sha[:12]} stamp={stamp.name}")

        print("[1c/7] Installing librarian-setup skill from pin…")
        if skill_src.is_dir():
            for skill_dir in (
                HERMES_HOME / "skills" / "librarian-setup",
                HERMES_HOME / "profiles" / "librarian" / "skills" / "librarian-setup",
            ):
                skill_dir.parent.mkdir(parents=True, exist_ok=True)
                if skill_dir.exists():
                    shutil.rmtree(skill_dir)
                shutil.copytree(skill_src, skill_dir)
                print(f"    Skill dir: {skill_dir}")
        else:
            print(f"    WARNING: {skill_src} not in pin — skip skill copy.")

        print("[2/7] pip install (non-editable) from pinned tree…")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", str(export)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    pip install failed: {result.stderr}", file=sys.stderr)
            return 1
        print("    Package installed from pin.")

        print("[3/7] Configuring Hermes config.yaml…")
        hermes_bin = shutil.which("hermes") or "hermes"
        for cmd, ok in (
            ([hermes_bin, "config", "set", "plugins.enabled", "['hybrid-age']"], "plugins.enabled set."),
            ([hermes_bin, "config", "set", "plugins.disabled", "['pgvector']"], "plugins.disabled set."),
        ):
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"    hermes config set failed: {result.stderr}")
            else:
                print(f"    {ok}")
        for profile_args, label in (
            ([], "default"),
            (["--profile", "librarian"], "librarian"),
        ):
            result = subprocess.run(
                [hermes_bin, *profile_args, "config", "set", "memory.provider", "hybrid-age"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"    memory.provider ({label}) via hermes CLI failed: {result.stderr.strip()[:200]}")
            else:
                print(f"    memory.provider=hybrid-age ({label})")

        print("[4/7] Writing hybrid_age block to config.yaml…")
        for config_path in CONFIG_PATHS:
            write_hybrid_age_block(config_path, embed_model, graph)
            print(f"    updated {config_path}")

        print(f"[5/7] Starting installed Postgres ({_redact_dsn(dsn)})…")
        compose_path = export / "docker-compose.yml"
        override_path = HERMES_HOME / "compose" / "hermes-memory-installed.yml"
        if not compose_path.exists():
            print("    WARNING: docker-compose.yml not in pin.")
        elif is_dev_clone_dsn(dsn):
            print("    --reuse-dsn: not creating a new compose project.")
        else:
            write_installed_compose_override(override_path)
            env = os.environ.copy()
            parsed = urlparse(dsn)
            if parsed.password:
                env["HERMES_PG_PASSWORD"] = parsed.password
            env["HERMES_PG_DB"] = INSTALLED_DB
            env["HERMES_PG_HOST_PORT"] = INSTALLED_PORT
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    INSTALLED_COMPOSE_PROJECT,
                    "-f",
                    str(compose_path),
                    "-f",
                    str(override_path),
                    "up",
                    "-d",
                    "postgres",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            if result.returncode != 0:
                print(f"    docker compose failed: {result.stderr}", file=sys.stderr)
                return 1
            print(f"    {INSTALLED_CONTAINER} on {INSTALLED_PORT}/{INSTALLED_DB}")

            print("[5b/7] Waiting for installed PostgreSQL…")
            max_wait = 60
            waited = 0
            while waited < max_wait:
                health = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Health.Status}}", INSTALLED_CONTAINER],
                    capture_output=True,
                    text=True,
                )
                if health.stdout.strip() == "healthy":
                    print("    PostgreSQL is healthy.")
                    break
                time.sleep(1)
                waited += 1
            else:
                print(f"    WARNING: {INSTALLED_CONTAINER} not healthy after {max_wait}s.")

            migrate = export / "scripts" / "migrate.py"
            if migrate.is_file():
                print("[5c/7] Applying pinned migrations…")
                mig = subprocess.run(
                    [sys.executable, str(migrate), "--dsn", dsn],
                    capture_output=True,
                    text=True,
                )
                print(mig.stdout)
                if mig.returncode != 0:
                    print(f"    migrate failed: {mig.stderr}", file=sys.stderr)
                    return 1

        print("[6/7] Restarting graph API (7890)…")
        try:
            from hermes_memory.graph_api import start_daemon, wait_ready

            start_daemon()
            if wait_ready(timeout=8.0):
                print("    graph API is up and serving.")
            else:
                print("    graph API may not be up yet. Log: /tmp/librarian_api.log")
        except Exception as exc:
            print(f"    API start failed: {exc}")

        print("[7/7] Running hermes-memory-verify…")
        result = subprocess.run(
            [sys.executable, "-m", "hermes_memory.verify"],
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.returncode == 0:
            print(f"Installation complete! pin={sha[:12]} dsn={_redact_dsn(dsn)} verify=PASS")
        else:
            print("Installation finished with verify: FAIL")
        print("    skipped hermes-memory-backfill (ABOUT/Concept only; not C–F).")
        return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
