#!/usr/bin/env python3
"""hermes-memory-install — First-time installation CLI."""

import argparse
import os
import time
import re
import shutil
import subprocess
import sys
from pathlib import Path

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
SETUP_SKILL_SRC = REPO_ROOT / "skills" / "librarian-setup"
SETUP_SKILL_DIRS = (
    HERMES_HOME / "skills" / "librarian-setup",
    HERMES_HOME / "profiles" / "librarian" / "skills" / "librarian-setup",
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
                config_text, flags=re.DOTALL | re.MULTILINE,
            )
        else:
            config_text = config_text.rstrip() + "\n" + block_text
        config_path.write_text(config_text)
    else:
        config_path.write_text(block_text)


def get_env_files():
    return (HERMES_HOME / ".env", HERMES_HOME / "profiles" / "librarian" / ".env")


def write_env_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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
    # merge updates
    for k, v in updates.items():
        if k not in existing:
            order.append(k)
        existing[k] = v
    # reconstruct preserving comments/blank-ish? simplest: keep comments + merged keys
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
                    # duplicate or already handled
                    if k not in seen:
                        out_lines.append(line)
                        seen.add(k)
            else:
                out_lines.append(line)
    # append any new keys not yet written
    for k in order:
        if k not in seen:
            out_lines.append(f"{k}={existing[k]}")
            seen.add(k)
    path.write_text("\n".join(out_lines) + "\n")


def main():
    parser = argparse.ArgumentParser(
        prog="hermes-memory-install",
        description="First-time installation CLI for the Hermes Librarian.",
    )
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN")
    parser.add_argument("--embed-url", default=None,
                        help="Ollama API URL")
    parser.add_argument("--embed-model", default=None,
                        help="Embedding model")
    parser.add_argument("--graph", default=None,
                        help="AGE graph name")
    parser.add_argument("--yes", action="store_true",
                        help="Assume yes to all prompts")
    args = parser.parse_args()

    dsn = args.dsn or os.environ.get("HYBRID_AGE_DSN")
    if not dsn:
        for env_path in get_env_files():
            try:
                for line in env_path.read_text().splitlines():
                    if line.startswith("HYBRID_AGE_DSN="):
                        dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                continue
            if dsn:
                break
    if not dsn and not args.yes:
        dsn = input("HYBRID_AGE_DSN: ").strip() or None
    if not dsn or "***" in dsn:
        if args.yes:
            print("ERROR: --yes requires --dsn or HYBRID_AGE_DSN with a real password.", file=sys.stderr)
            sys.exit(1)
        # Prompt for real password instead of storing placeholder
        import getpass
        pw = getpass.getpass("HYBRID_AGE_DSN password (will be embedded in DSN): ").strip()
        if pw:
            dsn = f"postgres://hermes:{pw}@localhost:5450/hermes_memory"
        elif dsn and "***" not in dsn:
            pass
        else:
            print("ERROR: A real Postgres password is required. Set HERMES_PG_PASSWORD or pass --dsn with a real password.", file=sys.stderr)
            print("       The placeholder '***' cannot be used — PostgreSQL will refuse to start/authenticate.", file=sys.stderr)
            sys.exit(1)
        # Also ensure HERMES_PG_PASSWORD is persisted for compose
        for env_path in get_env_files():
            try:
                if env_path.exists() and "HERMES_PG_PASSWORD" in env_path.read_text():
                    continue
            except OSError:
                pass
            merge_env_file(env_path, {"HERMES_PG_PASSWORD": pw}) if pw else None
    def _prompt(label, default, env_name):
        if args.yes:
            return os.environ.get(env_name) or default
        return input(f"{label}: ").strip() or default
    embed_url = args.embed_url or _prompt("HYBRID_AGE_EMBED_URL", "http://localhost:11434/v1", "HYBRID_AGE_EMBED_URL")
    embed_model = args.embed_model or _prompt("HYBRID_AGE_EMBED_MODEL", "nomic-embed-text", "HYBRID_AGE_EMBED_MODEL")
    graph = args.graph or _prompt("HYBRID_AGE_GRAPH", "hermes_knowledge", "HYBRID_AGE_GRAPH")

    # Write env files — merge, do not truncate unrelated keys
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

    # 1. Install plugins (default home + librarian profile)
    print("[1/7] Installing hybrid-age plugin...")
    src_dir = REPO_ROOT / "src" / "hermes_memory"
    for plugin_dir in PLUGIN_DIRS:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        for f in src_dir.glob("*.py"):
            shutil.copy2(str(f), str(plugin_dir / f.name))
        pc = plugin_dir / "__pycache__"
        if pc.exists():
            shutil.rmtree(str(pc))
        print(f"    Plugin dir: {plugin_dir}")

    print("[1b/7] Installing librarian-setup skill...")
    if SETUP_SKILL_SRC.is_dir():
        for skill_dir in SETUP_SKILL_DIRS:
            skill_dir.parent.mkdir(parents=True, exist_ok=True)
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            shutil.copytree(SETUP_SKILL_SRC, skill_dir)
            print(f"    Skill dir: {skill_dir}")
    else:
        print(f"    WARNING: {SETUP_SKILL_SRC} not found — skip skill copy.")

    # 2. Pip install
    print("[2/7] Installing package with pip...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    pip install failed: {result.stderr}")
        sys.exit(1)
    print("    Package installed.")

    # 3. Configure config.yaml via hermes CLI
    print("[3/7] Configuring Hermes config.yaml...")
    hermes_bin = shutil.which("hermes") or "hermes"
    result = subprocess.run(
        [hermes_bin, "config", "set", "plugins.enabled", "['hybrid-age']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.enabled set.")
    result = subprocess.run(
        [hermes_bin, "config", "set", "plugins.disabled", "['pgvector']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.disabled set.")
    for profile_args, label in (
        ([], "default"),
        (["--profile", "librarian"], "librarian"),
    ):
        result = subprocess.run(
            [hermes_bin, *profile_args, "config", "set", "memory.provider", "hybrid-age"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    memory.provider ({label}) via hermes CLI failed: {result.stderr.strip()[:200]}")
        else:
            print(f"    memory.provider=hybrid-age ({label})")

    # 4. Write hybrid_age block to default + librarian config.yaml
    print("[4/7] Writing hybrid_age block to config.yaml...")
    for config_path in CONFIG_PATHS:
        write_hybrid_age_block(config_path, embed_model, graph)
        print(f"    updated {config_path}")

    # 5. Start Docker compose
    print("[5/7] Starting Docker compose...")
    compose_path = REPO_ROOT / "docker-compose.yml"
    if compose_path.exists():
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "up", "-d"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    docker compose failed: {result.stderr}")
        else:
            print("    Docker compose started.")
    else:
        print("    WARNING: docker-compose.yml not found.")

    # Wait for health
    print("[5b/7] Waiting for PostgreSQL to become healthy...")
    max_wait = 60
    waited = 0
    while waited < max_wait:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path),
             "ps", "--filter", "name=hermes-memory-postgres",
             "--format", "{{.Status}}"],
            capture_output=True, text=True,
        )
        if "healthy" in result.stdout:
            print("    PostgreSQL is healthy.")
            break
        time.sleep(1)
        waited += 1
    else:
        print(f"    WARNING: PostgreSQL not healthy after {max_wait}s. "
               "Continuing anyway.")

    # 6. Restart viz API on 127.0.0.1:7890
    print("[6/7] Restarting graph API (7890)...")
    try:
        from hermes_memory.graph_api import start_daemon, wait_ready
        start_daemon()
        if wait_ready(timeout=8.0):
            print("    graph API is up and serving.")
        else:
            print("    graph API may not be up yet. Log: /tmp/librarian_api.log")
    except Exception as e:
        print(f"    API start failed: {e}")

    # 7. Run verification — use python -m to be cwd-independent
    print("[7/7] Running hermes-memory-verify...")
    result = subprocess.run([sys.executable, "-m", "hermes_memory.verify"],
                            capture_output=True, text=True)
    print(result.stdout)
    if result.returncode == 0:
        print("Installation complete! Verify: PASS")
    else:
        print("Installation finished with verify: FAIL")
    print("[8] Backfilling conversation graph from existing rows...")
    bf = subprocess.run(
        [sys.executable, "-m", "hermes_memory.backfill"],
        capture_output=True, text=True,
    )
    print(bf.stdout)
    if bf.returncode != 0:
        print(f"    backfill warning: {bf.stderr[:300]}")


if __name__ == "__main__":
    main()
