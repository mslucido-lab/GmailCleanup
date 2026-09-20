"""Shared SQLite database contract for Gmail Cleanup."""

from .connection import connect
from .migrate import migrate

__all__ = ("connect", "migrate")
