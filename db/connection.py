"""Connection configuration shared by every local process."""

from __future__ import annotations

import sqlite3
from pathlib import Path


DEFAULT_BUSY_TIMEOUT_MS = 5_000


def connect(database_path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a configured database connection.

    WAL permits the review backend to serve reads while the executor records
    progress.  Foreign-key enforcement is connection-local in SQLite, so it is
    deliberately enabled here rather than relying on a one-time migration.
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1_000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return connection
