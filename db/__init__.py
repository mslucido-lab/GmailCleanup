"""Shared SQLite database contract for Gmail Cleanup."""

from .connection import connect
from .migrate import migrate
from .snapshots import confirmation_snapshot_hash

__all__ = ("connect", "migrate", "confirmation_snapshot_hash")
