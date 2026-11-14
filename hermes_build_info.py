"""Build provenance for setup.py: the commit a wheel was built from.

Checkouts read it from Git. A ``git archive`` export has no ``.git``, so the
installer writes the pinned commit into ``PIN_FILE`` and it is used instead.
Without either, the commit is recorded as ``unknown`` and the installer refuses
to pin that build.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PIN_FILE = "HERMES_PIN.json"


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def build_info(root: Path) -> dict:
    sha = _git(root, "rev-parse", "HEAD")
    if sha is None:
        pin = root / PIN_FILE
        if pin.is_file():
            data = json.loads(pin.read_text(encoding="utf-8"))
            pinned = str(data["sha"])
            return {"sha": pinned, "ref": str(data.get("ref") or pinned[:12]),
                    "dirty": bool(data.get("dirty", False))}
        return {"sha": "unknown", "ref": "unknown", "dirty": False}
    exact_tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    return {"sha": sha, "ref": exact_tag or sha[:12], "dirty": dirty}
