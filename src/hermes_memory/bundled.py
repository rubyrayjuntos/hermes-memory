"""Install assets bundled into the wheel, so installing needs no Git checkout.

``setup.py`` copies the Docker build context, migrations, migrate script and
setup skill into ``hermes_memory/_bundle`` at build time and writes
``build_info.json`` (commit id, ref, dirty flag) from the build's Git state.
Dev checkouts keep the Git-pinned path in ``install_cli``.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent / "_bundle"
BUILD_INFO = "build_info.json"
BUNDLED_TREES = ("docker", "sql", "scripts", "skills")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def is_checkout(repo: Path) -> bool:
    return (repo / ".git").exists()


def read_build_info(bundle: Path = BUNDLE_DIR) -> dict:
    try:
        info = json.loads((bundle / BUILD_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit(
            "install: this hermes-memory package has no bundled build information. "
            "Install a release built from a Git checkout (pip install git+https://github.com/"
            "rubyrayjuntos/hermes-memory@<tag>)."
        ) from None
    if not isinstance(info, dict):
        raise SystemExit("install: bundled build information is malformed.")
    return info


def resolve_bundle_pin(bundle: Path = BUNDLE_DIR) -> tuple[str, str]:
    """Return ``(ref, full_sha)`` recorded when this package was built."""
    info = read_build_info(bundle)
    sha = str(info.get("sha", ""))
    if not _FULL_SHA.fullmatch(sha):
        raise SystemExit(
            "install: this package was built without a commit id, so it cannot be pinned. "
            "Install a release built from a Git checkout."
        )
    if info.get("dirty"):
        raise SystemExit(
            "install: this package was built from uncommitted changes, so it cannot be pinned. "
            "Build from a clean commit."
        )
    return str(info.get("ref") or sha[:12]), sha


def export_bundle(dest: Path, bundle: Path = BUNDLE_DIR) -> None:
    """Lay out the bundled trees under ``dest`` at the same relative paths as the repo."""
    for rel in BUNDLED_TREES:
        source = bundle / rel
        if source.exists():
            shutil.copytree(source, dest / rel)
