#!/usr/bin/env python3
"""hermes-memory-install — First-time installation CLI."""

import argparse
import time
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path.home() / ".hermes"


def get_env_files():
    return (HERMES_HOME / ".env", HERMES_HOME / "profiles" / "librarian" / ".env")


def write_env_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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

    dsn = args.dsn or input("HYBRID_AGE_DSN: ").strip() or \
        "postgres://hermes:***@localhost:5450/hermes_memory"
    embed_url = args.embed_url or input("HYBRID_AGE_EMBED_URL: ").strip() or \
        "http://localhost:11434/v1"
    embed_model = args.embed_model or input("HYBRID_AGE_EMBED_MODEL: ").strip() or \
        "nomic-embed-text"
    graph = args.graph or input("HYBRID_AGE_GRAPH: ").strip() or "hermes_knowledge"

    # Write env files
    for env_path in get_env_files():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        txt = (f"HYBRID_AGE_DSN={dsn}\n"
               f"HYBRID_AGE_EMBED_URL={embed_url}\n"
               f"HYBRID_AGE_EMBED_MODEL={embed_model}\n"
               f"HYBRID_AGE_GRAPH={graph}\n")
        write_env_file(env_path, txt)

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
    result = subprocess.run(
        ["hermes", "config", "set", "plugins.enabled", "['hybrid-age']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.enabled set.")
    result = subprocess.run(
        ["hermes", "config", "set", "plugins.disabled", "['pgvector']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.disabled set.")

    # 4. Write hybrid_age block to config.yaml
    print("[4/7] Writing hybrid_age block to config.yaml...")
    config_path = HERMES_HOME / "config.yaml"
    new_block = (f"\nhybrid_age:\n"
                 f"  dsn_env: HYBRID_AGE_DSN\n"
                 f"  embed_url_env: HYBRID_AGE_EMBED_URL\n"
                 f"  embed_model: {embed_model}\n"
                 f"  graph: {graph}\n"
                 f"  vector_k: 12\n"
                 f"  min_similarity: 0.55\n"
                 f"  max_tokens: 1200\n")
    if config_path.exists():
        with open(config_path, 'r') as f:
            config_text = f.read()
        if "hybrid_age:" in config_text:
            config_text = re.sub(r"hybrid_age:.*?(?=\n[\w\W]*$)",
                                 new_block, config_text, flags=re.DOTALL)
        else:
            config_text = config_text.rstrip() + new_block
    else:
        config_text = new_block
    with open(config_path, 'w') as f:
        f.write(config_text)
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
    api_script = str(REPO_ROOT / "scripts" / "graph_api.py")
    subprocess.run(["nohup", "python3", api_script,
                    "> /tmp/librarian_api.log 2>&1 &"],
                    capture_output=True)
    print("    API startup initiated.")
    time.sleep(3)
    result = subprocess.run(["curl", "-s",
                            "http://127.0.0.1:7890/api/librarian/graph/stats?fresh=1"],
                            capture_output=True, text=True)
    if result.returncode == 0 and len(result.stdout) > 100:
        print("    Flask API is up and serving.")
    else:
        print(f"    Flask API may not be up yet. Log: {result.stdout[:200]}")

    # 7. Run verification
    print("[7/7] Running hermes-memory-verify...")
    result = subprocess.run(["hermes-memory-verify"],
                            capture_output=True, text=True)
    print(result.stdout)
    if result.returncode == 0:
        print("Installation complete! Verify: PASS")
    else:
        print("Installation finished with verify: FAIL")


if __name__ == "__main__":
    main()