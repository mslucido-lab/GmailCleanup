"""Immutable batch snapshots shared by review-ui and execute."""
from __future__ import annotations

import hashlib
import sqlite3


def confirmation_snapshot_hash(connection: sqlite3.Connection, batch_id: str) -> str:
    """Hash exactly the labeled message set and approval-time batch totals.

    This is the canonical cross-process definition.  Do not reimplement it in
    the UI or executor: both must call this function before/after confirmation.
    """
    batch = connection.execute(
        "SELECT message_count,total_size_bytes FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if not batch:
        raise ValueError(f"Unknown batch: {batch_id}")
    ids = [row[0] for row in connection.execute(
        "SELECT message_id FROM batch_messages WHERE batch_id=? AND status='labeled' ORDER BY message_id",
        (batch_id,),
    )]
    payload = "\n".join(ids) + f"\n{batch['message_count']}\n{batch['total_size_bytes']}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
