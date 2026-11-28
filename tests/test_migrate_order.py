"""scripts/migrate.py must apply migrations in numeric order, not string
order. Exposed by V24 (ALTER TABLE noun ...), which depends on V9 (CREATE
TABLE noun ...) -- lexicographic sort puts V10..V23 and V24 BEFORE V2..V9,
so V24 ran before the table it alters even existed."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import migrate  # noqa: E402


def test_list_migrations_sorts_numerically_not_lexicographically():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # Deliberately out of numeric order on disk; lexicographic sort of
        # these filenames would put V10/V2/V24/V9 in the wrong relative
        # order ("V10" < "V2" < "V24" < "V9" as strings).
        for name in ("V9__noun.sql", "V2__a.sql", "V24__noun_slug.sql", "V10__x.sql", "V1__base.sql"):
            (d / name).write_text("-- noop\n")

        migrations = migrate.list_migrations(d)
        versions = [v for v, _, _, _, _ in migrations]
        assert versions == ["V1", "V2", "V9", "V10", "V24"], (
            f"expected numeric order V1,V2,V9,V10,V24 but got {versions}"
        )


def test_list_migrations_real_migrations_dir_has_v9_before_v24():
    """The real repro: V9 creates `noun`, V24 ALTERs it. V9 must run first."""
    migrations = migrate.list_migrations(REPO_ROOT / "sql" / "migrations")
    versions = [v for v, _, _, _, _ in migrations]
    assert versions.index("V9") < versions.index("V24"), (
        f"V9 (creates noun) must apply before V24 (alters noun): {versions}"
    )
