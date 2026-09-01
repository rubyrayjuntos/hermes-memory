#!/usr/bin/env python3
"""hermes-memory-install — First-time installation CLI."""

import argparse
import time
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_HOME = Path.home() / ".hermes"


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

    dsn = args.dsn or input("HYBRID_AGE_DSN: ").strip() or None
    if not dsn or "***" in dsn:
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
    embed_url = args.embed_url or input("HYBRID_AGE_EMBED_URL: ").strip() or \
        "http://localhost:11434/v1"
    embed_model = args.embed_model or input("HYBRID_AGE_EMBED_MODEL: ").strip() or \
        "nomic-embed-text"
    graph = args.graph or input("HYBRID_AGE_GRAPH: ").strip() or "hermes_knowledge"

    # Write env files — merge, do not truncate unrelated keys
    updates = {
        "HYBRID_AGE_DSN": dsn,
        "HYBRID_AGE_EMBED_URL": embed_url,
        "HYBRID_AGE_EMBED_MODEL": embed_model,
        "HYBRID_AGE_GRAPH": graph,
    }
    for env_path in get_env_files():
        merge_env_file(env_path, updates)

    # 1. Install plugins
    print("[1/7] Installing hybrid-age plugin...")
    plugin_dir = HERMES_HOME / "plugins" / "hybrid-age"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    src_dir = REPO_ROOT / "src" / "hermes_memory"
    for f in src_dir.glob("*.py"):
        shutil.copy2(str(f), str(plugin_dir / f.name))
    init_src = src_dir / "__init__.py"
    init_dst = plugin_dir / "__init__.py"
    if init_src.exists():
        shutil.copy2(str(init_src), str(init_dst))
    pc = plugin_dir / "__pycache__"
    if pc.exists():
        shutil.rmtree(str(pc))
    print(f"    Plugin dir: {plugin_dir}")

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

    # 4. Write hybrid_age block to config.yaml — parse YAML if available
    print("[4/7] Writing hybrid_age block to config.yaml...")
    config_path = HERMES_HOME / "config.yaml"
    new_block_dict = {
        "dsn_env": "HYBRID_AGE_DSN",
        "embed_url_env": "HYBRID_AGE_EMBED_URL",
        "embed_model": embed_model,
        "graph": graph,
        "vector_k": 12,
        "min_similarity": 0.55,
        "max_tokens": 1200,
    }
    try:
        import yaml  # type: ignore
        has_yaml = True
    except ImportError:
        has_yaml = False
    if config_path.exists():
        if has_yaml:
            try:
                with open(config_path, 'r') as f:
                    cfg = yaml.safe_load(f) or {}
                cfg["hybrid_age"] = new_block_dict
                with open(config_path, 'w') as f:
                    yaml.safe_dump(cfg, f, sort_keys=False)
            except Exception as e:
                print(f"    WARNING: YAML update failed ({e}), falling back to text append")
                has_yaml = False
        if not has_yaml:
            with open(config_path, 'r') as f:
                config_text = f.read()
            if "hybrid_age:" in config_text:
                # Replace through next top-level key or EOF (properly)
                config_text = re.sub(
                    r"^hybrid_age:.*?(?=^\w|\Z)",
                    lambda m: f"hybrid_age:\n  dsn_env: HYBRID_AGE_DSN\n  embed_url_env: HYBRID_AGE_EMBED_URL\n  embed_model: {embed_model}\n  graph: {graph}\n  vector_k: 12\n  min_similarity: 0.55\n  max_tokens: 1200\n",
                    config_text, flags=re.DOTALL | re.MULTILINE
                )
                # If regex didn't consume old indented values (fallback), ensure single block
                if config_text.count("hybrid_age:") > 1:
                    # keep first occurrence, remove duplicates
                    parts = config_text.split("hybrid_age:")
                    config_text = parts[0] + "hybrid_age:" + parts[1].split("\nhybrid_age:")[0]
                    # rebuild with correct block if needed
                    if f"graph: {graph}" not in config_text:
                        config_text = re.sub(r"^hybrid_age:.*?(?=^\w|\Z)",
                                             f"hybrid_age:\n  dsn_env: HYBRID_AGE_DSN\n  embed_url_env: HYBRID_AGE_EMBED_URL\n  embed_model: {embed_model}\n  graph: {graph}\n  vector_k: 12\n  min_similarity: 0.55\n  max_tokens: 1200\n",
                                             config_text, flags=re.DOTALL | re.MULTILINE)
            else:
                config_text = config_text.rstrip() + f"\nhybrid_age:\n  dsn_env: HYBRID_AGE_DSN\n  embed_url_env: HYBRID_AGE_EMBED_URL\n  embed_model: {embed_model}\n  graph: {graph}\n  vector_k: 12\n  min_similarity: 0.55\n  max_tokens: 1200\n"
            if not has_yaml:
                with open(config_path, 'w') as f:
                    f.write(config_text)
    else:
        if has_yaml:
            with open(config_path, 'w') as f:
                yaml.safe_dump({"hybrid_age": new_block_dict}, f, sort_keys=False)
        else:
            with open(config_path, 'w') as f:
                f.write(f"hybrid_age:\n  dsn_env: HYBRID_AGE_DSN\n  embed_url_env: HYBRID_AGE_EMBED_URL\n  embed_model: {embed_model}\n  graph: {graph}\n  vector_k: 12\n  min_similarity: 0.55\n  max_tokens: 1200\n")
    print("    config.yaml updated.")

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

    # 6. Restart Flask 7890 API
    print("[6/7] Restarting Flask API (7890)...")
    subprocess.run(["pkill", "-f", "graph_api.py"], capture_output=True)
    time.sleep(1)
    api_script = REPO_ROOT / "scripts" / "graph_api.py"
    try:
        log_path = Path("/tmp/librarian_api.log")
        # Use Popen with absolute python, not shell redirection literals
        subprocess.Popen(
            [sys.executable, str(api_script)],
            stdout=open(log_path, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        print(f"    API start failed: {e}")
    print("    API startup initiated.")
    time.sleep(3)
    result = subprocess.run(["curl", "-s",
                            "http://127.0.0.1:7890/api/librarian/graph/stats?fresh=1"],
                            capture_output=True, text=True)
    if result.returncode == 0 and len(result.stdout) > 100:
        print("    Flask API is up and serving.")
    else:
        print(f"    Flask API may not be up yet. Log: {result.stdout[:200]}")

    # 7. Run verification — use python -m to be cwd-independent
    print("[7/7] Running hermes-memory-verify...")
    result = subprocess.run([sys.executable, "-m", "hermes_memory.verify"],
                            capture_output=True, text=True)
    print(result.stdout)
    if result.returncode == 0:
        print("Installation complete! Verify: PASS")
    else:
        print("Installation finished with verify: FAIL")


if __name__ == "__main__":
    main()
