"""Pytest bootstrap: put the repo root (for tests.strategies) and src/ on sys.path once.

Removes the per-file ``sys.path.insert`` hack previously repeated in every
tests/property file. pytest imports this conftest before collecting test
modules, so plain ``from strategies import ...`` works everywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT / "tests", _REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
