"""Small, sequential migration runner for the local SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .connection import connect


MODULE_DIR = Path(__file__).resolve().parent
INITIAL_SCHEMA = MODULE_DIR / "schema.sql"
MIGRATIONS_DIR = MODULE_DIR / "migrations"
FOREIGN_KEYS_OFF_MARKER = "-- migrate: foreign_keys_off"


def _available_migrations() -> Iterable[tuple[int, Path]]:
    """Yield the initial schema followed by numbered, forward-only migrations."""
    yield 1, INITIAL_SCHEMA
    if not MIGRATIONS_DIR.exists():
        return

    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        yield int(path.name[:3]), path


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not exists:
        return set()
    return {row[0] for row in connection.execute("SELECT version FROM schema_version")}


def migrate(connection: sqlite3.Connection) -> list[int]:
    """Apply each unapplied migration once, in its own transaction.

    Migrations are intentionally append-only.  The first migration is the
    canonical ``schema.sql`` contract; later migrations belong in
    ``db/migrations/NNN_description.sql``.
    """
    applied = _applied_versions(connection)
    ran: list[int] = []

    for version, path in _available_migrations():
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        requires_foreign_keys_off = sql.lstrip().startswith(FOREIGN_KEYS_OFF_MARKER)
        escaped_path = str(path.name).replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            "INSERT INTO schema_version (version, applied_at, source) "
            f"VALUES ({version}, unixepoch(), '{escaped_path}');\n"
            "COMMIT;"
        )
        try:
            if requires_foreign_keys_off:
                # Rebuilding a referenced table requires this outside the
                # migration transaction; SQLite ignores this pragma in one.
                connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript(script)
        except Exception:
            connection.rollback()
            raise
        finally:
            if requires_foreign_keys_off:
                connection.execute("PRAGMA foreign_keys = ON")
        ran.append(version)

    return ran


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Gmail Cleanup SQLite migrations.")
    parser.add_argument("database", type=Path, help="Path to the SQLite database file")
    args = parser.parse_args()
    connection = connect(args.database)
    try:
        ran = migrate(connection)
    finally:
        connection.close()
    print("Applied migrations: " + (", ".join(map(str, ran)) if ran else "none"))


if __name__ == "__main__":
    main()
