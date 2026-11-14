"""Diff two claim-key sets from a re-extraction (graph layer spec §5.2 step 5).

Pure. Keys are ``"{subject}|{verb}|{object}|{polarity}"``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiffReport:
    lost: tuple[str, ...]
    added: tuple[str, ...]


def diff_claims(before: set[str], after: set[str]) -> DiffReport:
    return DiffReport(
        lost=tuple(sorted(before - after)),
        added=tuple(sorted(after - before)),
    )
