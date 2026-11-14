"""Build hook: bundle install assets into the wheel and record the build's commit.

Metadata lives in pyproject.toml. This file only adds ``hermes_memory/_bundle``
(generated, gitignored) before setuptools collects package data.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

ROOT = Path(__file__).resolve().parent

# Load by path: pip's in-process backend does not put this directory on sys.path.
_spec = importlib.util.spec_from_file_location("hermes_build_info", ROOT / "hermes_build_info.py")
_build_info_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_build_info_module)
build_info = _build_info_module.build_info
BUNDLE = ROOT / "src" / "hermes_memory" / "_bundle"
SOURCES = ("docker", "sql/migrations", "scripts/migrate.py", "skills/librarian-setup")


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
        (BUNDLE / "build_info.json").write_text(json.dumps(build_info(ROOT), indent=2) + "\n", encoding="utf-8")
        super().run()


setup(cmdclass={"build_py": build_py})
