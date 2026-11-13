"""Build hook: bundle install assets into the wheel and record the build's commit.

Metadata lives in pyproject.toml. This file only adds ``hermes_memory/_bundle``
(generated, gitignored) before setuptools collects package data.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / "src" / "hermes_memory" / "_bundle"
SOURCES = ("docker", "sql/migrations", "scripts/migrate.py", "skills/librarian-setup")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def build_info() -> dict:
    sha = _git("rev-parse", "HEAD") or "unknown"
    exact_tag = _git("describe", "--tags", "--exact-match", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return {"sha": sha, "ref": exact_tag or sha[:12], "dirty": dirty}


class build_py(_build_py):
    def run(self) -> None:
        if BUNDLE.exists():
            shutil.rmtree(BUNDLE)
        for rel in SOURCES:
            source, target = ROOT / rel, BUNDLE / rel
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        (BUNDLE / "build_info.json").write_text(json.dumps(build_info(), indent=2) + "\n", encoding="utf-8")
        super().run()


setup(cmdclass={"build_py": build_py})
