"""Resumable, preflight-gated Gmail archive, restore, and Trash operations."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterable, Sequence
from itertools import islice
from typing import Any

from db import confirmation_snapshot_hash

from .gateway import GmailModifyGateway

try:  # These are installed with the Google API client, but keep core imports light for fixtures.
    from google.auth.exceptions import TransportError
    from httplib2.error import HttpLib2Error
    GOOGLE_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (TransportError, HttpLib2Error)
except ImportError:  # pragma: no cover - exercised only in minimal dependency environments.
    GOOGLE_TRANSPORT_ERRORS = ()


MAX_GMAIL_BATCH_SIZE = 50
DEFAULT_GMAIL_BATCH_SIZE = 10
ARCHIVE_LABEL_NAME = "Cleanup/Archive"
INBOX_LABEL_ID = "INBOX"
TRASH_LABEL_ID = "TRASH"


def chunks(values: Sequence[str], size: int = DEFAULT_GMAIL_BATCH_SIZE) -> Iterable[list[str]]:
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


class Executor:
    def __init__(
        self,
        connection: sqlite3.Connection,
        gateway: GmailModifyGateway,
        *,
        protected_label_names: Sequence[str],
        restore_window_days: int = 30,
        batch_size_cap: int = 5_000,
        gmail_batch_size: int = DEFAULT_GMAIL_BATCH_SIZE,
        gmail_batch_interval_seconds: float = 2.0,
        max_attempts: int = 5,
        sleep: Any = time.sleep,
    ) -> None:
        self.connection = connection
        self.gateway = gateway
        self.protected_label_names = tuple(protected_label_names)
        self.restore_window_days = restore_window_days
        self.batch_size_cap = batch_size_cap
        if not 1 <= gmail_batch_size <= MAX_GMAIL_BATCH_SIZE:
            raise ValueError(f"gmail_batch_size must be between 1 and {MAX_GMAIL_BATCH_SIZE}")
        if gmail_batch_interval_seconds < 0:
            raise ValueError("gmail_batch_interval_seconds cannot be negative")
        self.gmail_batch_size = gmail_batch_size
        self.gmail_batch_interval_seconds = gmail_batch_interval_seconds
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.protected_label_ids: set[str] = set()
        self.archive_label_id = ""

    def provision_labels(self, *, allow_create: bool) -> None:
        """Resolve Mark-owned labels fail-closed and provision only the tool label."""
        labels = {label["name"]: label["id"] for label in self._request(self.gateway.list_labels)}
        missing = [name for name in self.protected_label_names if name not in labels]
        if missing:
            raise ValueError("Configured protected labels do not exist: " + ", ".join(missing))
        if ARCHIVE_LABEL_NAME not in labels:
            if not allow_create:
                raise ValueError(f"{ARCHIVE_LABEL_NAME} does not exist; run with --live to provision it")
            created = self._request(lambda: self.gateway.create_label(ARCHIVE_LABEL_NAME))
            labels[ARCHIVE_LABEL_NAME] = created["id"]
        now = int(time.time())
        with self.connection:
            for name in (*self.protected_label_names, ARCHIVE_LABEL_NAME):
                self.connection.execute(
                    """INSERT INTO label_map (label_name,label_id,resolved_at) VALUES (?,?,?)
                       ON CONFLICT(label_name) DO UPDATE SET label_id=excluded.label_id,resolved_at=excluded.resolved_at""",
                    (name, labels[name], now),
                )
        # STARRED is always a live safety rail even if someone removes it from
        # the configurable protected-label list.
        self.protected_label_ids = {labels[name] for name in self.protected_label_names} | {"STARRED"}
        self.archive_label_id = labels[ARCHIVE_LABEL_NAME]

    def archive(self, batch_id: str, *, dry_run: bool = True) -> dict[str, int]:
        batch = self._batch(batch_id)
        if batch["status"] not in {"approved", "labeling", "failed"}:
            raise ValueError("Only approved or resumable archive batches can be archived")
        if batch["status"] == "failed" and not self._message_ids(batch_id, "pending"):
            raise ValueError("Failed batch has no pending archive messages")
        if dry_run:
            return {"planned": min(len(self._message_ids(batch_id, "pending")), self.batch_size_cap)}
        if batch["status"] != "labeling":
            self._set_batch_status(batch_id, "labeling")
        result = self._write_archive(batch_id)
        if not self._message_ids(batch_id, "pending"):
            self._finish_archive(batch_id)
        return result

    def restore(self, batch_id: str, *, dry_run: bool = True) -> dict[str, int]:
        batch = self._batch(batch_id)
        if batch["status"] not in {"restore_window", "restoring", "failed"}:
            raise ValueError("Only restore-window batches can be restored")
        ids = self._message_ids(batch_id, "labeled")
        if dry_run:
            return {"planned": min(len(ids), self.batch_size_cap)}
        if batch["status"] != "restoring":
            self._claim(batch_id, "restoring", ("restore_window", "failed"), "Only restore-window batches can be restored")
        result = {"restored": 0, "excluded": 0}
        for ids_chunk in chunks(ids[: self.batch_size_cap], self.gmail_batch_size):
            live = self._preflight(batch_id, ids_chunk, operation="restore")
            protected = self._protected(live)
            # Do not alter a newly-protected message; leave it retryable.
            if protected:
                result["excluded"] += len(protected)
            operations: dict[tuple[str, ...], list[str]] = {}
            for message_id in ids_chunk:
                if message_id in protected:
                    continue
                row = self.connection.execute(
                    "SELECT original_labels FROM batch_messages WHERE batch_id=? AND message_id=?", (batch_id, message_id)
                ).fetchone()
                if not row or row["original_labels"] is None:
                    error = RuntimeError(f"Missing original-label snapshot for {message_id}")
                    self._fail(batch_id, error, operation="restore")
                    raise error
                original = json.loads(row["original_labels"])
                add = tuple(sorted(set(original) - set(live[message_id])))
                operations.setdefault(add, []).append(message_id)
            for add, message_ids in operations.items():
                self._gmail_write(batch_id, message_ids, operation="restore", add_label_ids=add, remove_label_ids=[self.archive_label_id])
                result["restored"] += len(message_ids)
        if not self._message_ids(batch_id, "labeled") or result["restored"] == len(ids):
            self._finish_restore(batch_id, result["restored"])
        return result

    def trash(self, batch_id: str, *, dry_run: bool = True) -> dict[str, int]:
        batch = self._batch(batch_id)
        if batch["status"] not in {"restore_window", "trashing", "failed"}:
            raise ValueError("Only confirmed restore-window batches can move to Trash")
        if batch["status"] == "failed" and not self._message_ids(batch_id, "labeled"):
            raise ValueError("Failed batch has no pending Trash messages")
        if not batch["permanent_delete_confirmed_at"]:
            raise ValueError("A separate Trash confirmation is required")
        if batch["restore_deadline"] is None or batch["restore_deadline"] >= time.time():
            raise ValueError("The restore window has not closed")
        if confirmation_snapshot_hash(self.connection, batch_id) != batch["confirmation_snapshot_hash"]:
            with self.connection:
                self.connection.execute(
                    "UPDATE batches SET permanent_delete_confirmed_at=NULL, confirmation_snapshot_hash=NULL WHERE batch_id=?",
                    (batch_id,),
                )
            raise ValueError("Confirmation snapshot changed and has been invalidated")
        if dry_run:
            return {"planned": min(len(self._message_ids(batch_id, "labeled")), self.batch_size_cap)}
        if batch["status"] != "trashing":
            self._claim(batch_id, "trashing", ("restore_window", "failed"), "Only confirmed restore-window batches can move to Trash")
        result = self._write_trash(batch_id)
        if not self._message_ids(batch_id, "labeled"):
            self._finish_trash(batch_id)
        return result

    def _write_archive(self, batch_id: str) -> dict[str, int]:
        result = {"labeled": 0, "excluded": 0}
        for ids_chunk in chunks(self._message_ids(batch_id, "pending")[: self.batch_size_cap], self.gmail_batch_size):
            live = self._preflight(batch_id, ids_chunk, operation="archive")
            protected = self._protected(live)
            if protected:
                self._set_messages(batch_id, protected, "excluded_protected", from_status="pending")
                result["excluded"] += len(protected)
            allowed = [message_id for message_id in ids_chunk if message_id not in protected]
            if not allowed:
                continue
            # This is deliberately its own committed transaction before Gmail.
            self._snapshot_original_labels(batch_id, allowed, live)
            self._gmail_write(batch_id, allowed, operation="archive", add_label_ids=[self.archive_label_id], remove_label_ids=[INBOX_LABEL_ID])
            self._set_messages(batch_id, allowed, "labeled", from_status="pending")
            result["labeled"] += len(allowed)
        return result

    def _write_trash(self, batch_id: str) -> dict[str, int]:
        result = {"trashed": 0, "excluded": 0}
        for ids_chunk in chunks(self._message_ids(batch_id, "labeled")[: self.batch_size_cap], self.gmail_batch_size):
            live = self._preflight(batch_id, ids_chunk, operation="trash")
            protected = self._protected(live)
            if protected:
                self._set_messages(batch_id, protected, "excluded_protected", from_status="labeled")
                result["excluded"] += len(protected)
            allowed = [message_id for message_id in ids_chunk if message_id not in protected]
            if not allowed:
                continue
            self._gmail_write(batch_id, allowed, operation="trash", add_label_ids=[TRASH_LABEL_ID], remove_label_ids=[self.archive_label_id])
            self._set_messages(batch_id, allowed, "trashed", from_status="labeled")
            result["trashed"] += len(allowed)
        return result

    def _preflight(self, batch_id: str, ids: Sequence[str], *, operation: str) -> dict[str, list[str]]:
        try:
            result = self._request(lambda: self.gateway.get_current_labels(ids))
            self._pace()
            return result
        except Exception as error:
            self._fail(batch_id, error, operation=operation)
            raise

    def _protected(self, labels: dict[str, list[str]]) -> set[str]:
        return {message_id for message_id, label_ids in labels.items() if set(label_ids) & self.protected_label_ids}

    def _snapshot_original_labels(self, batch_id: str, ids: Sequence[str], live: dict[str, list[str]]) -> None:
        with self.connection:
            self.connection.executemany(
                """UPDATE batch_messages SET original_labels=?
                   WHERE batch_id=? AND message_id=? AND original_labels IS NULL AND status='pending'""",
                [(json.dumps(live[message_id]), batch_id, message_id) for message_id in ids],
            )

    def _gmail_write(self, batch_id: str, ids: Sequence[str], *, operation: str, add_label_ids: Sequence[str], remove_label_ids: Sequence[str]) -> None:
        try:
            self._request(lambda: self.gateway.batch_modify(ids, add_label_ids=add_label_ids, remove_label_ids=remove_label_ids))
            self._pace()
        except Exception as error:
            # Keep every row at its last confirmed state; retry is idempotent.
            self._fail(batch_id, error, operation=operation)
            raise

    def _batch(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown batch: {batch_id}")
        return row

    def _message_ids(self, batch_id: str, status: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT message_id FROM batch_messages WHERE batch_id=? AND status=? ORDER BY message_id", (batch_id, status)
        )]

    def _set_batch_status(self, batch_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE batches SET status=? WHERE batch_id=?", (status, batch_id))

    def _claim(self, batch_id: str, status: str, allowed_from: Sequence[str], message: str) -> None:
        """Claim a live Gmail action without holding a SQLite transaction over I/O."""
        placeholders = ",".join("?" for _ in allowed_from)
        with self.connection:
            result = self.connection.execute(
                f"UPDATE batches SET status=? WHERE batch_id=? AND status IN ({placeholders})",
                (status, batch_id, *allowed_from),
            )
        if result.rowcount != 1:
            raise ValueError(message)

    def _set_messages(self, batch_id: str, ids: Sequence[str], status: str, *, from_status: str) -> None:
        with self.connection:
            self.connection.executemany(
                "UPDATE batch_messages SET status=? WHERE batch_id=? AND message_id=? AND status=?",
                [(status, batch_id, message_id, from_status) for message_id in ids],
            )

    def _finish_archive(self, batch_id: str) -> None:
        now = int(time.time())
        count = self.connection.execute(
            "SELECT COUNT(*) FROM batch_messages WHERE batch_id=? AND status IN ('labeled','excluded_protected')", (batch_id,)
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                """UPDATE batches SET status='restore_window', labeled_at=?, restore_deadline=? WHERE batch_id=?""",
                (now, now + self.restore_window_days * 86_400, batch_id),
            )
            self.connection.execute(
                "INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'labeled',?,?,'')",
                (batch_id, count, now),
            )

    def _finish_restore(self, batch_id: str, count: int) -> None:
        now = int(time.time())
        with self.connection:
            self.connection.execute("UPDATE batches SET status='restored', restored_at=? WHERE batch_id=?", (now, batch_id))
            self.connection.execute(
                "INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'restored',?,?,'')",
                (batch_id, count, now),
            )

    def _finish_trash(self, batch_id: str) -> None:
        now = int(time.time())
        count = self.connection.execute(
            "SELECT COUNT(*) FROM batch_messages WHERE batch_id=? AND status='trashed'", (batch_id,)
        ).fetchone()[0]
        with self.connection:
            self.connection.execute("UPDATE batches SET status='trashed', trashed_at=? WHERE batch_id=?", (now, batch_id))
            self.connection.execute(
                "INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'moved_to_trash',?,?,'')",
                (batch_id, count, now),
            )

    def _fail(self, batch_id: str, error: Exception | None = None, *, operation: str | None = None) -> None:
        note = "" if error is None else f"{type(error).__name__}: {error}"
        if operation:
            note = f"operation={operation}; {note}"
        count = self.connection.execute(
            "SELECT COUNT(*) FROM batch_messages WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        with self.connection:
            self.connection.execute("UPDATE batches SET status='failed' WHERE batch_id=?", (batch_id,))
            self.connection.execute(
                "INSERT INTO audit_log (batch_id,event,message_count,timestamp,note) VALUES (?,'failed',?,unixepoch(),?)",
                (batch_id, count, note),
            )

    def _pace(self) -> None:
        """Keep each live preflight and write call below Gmail's burst tolerance."""
        if self.gmail_batch_interval_seconds:
            self.sleep(self.gmail_batch_interval_seconds)

    def _request(self, operation: Any) -> Any:
        for attempt in range(self.max_attempts):
            try:
                return operation()
            except Exception as error:
                if not self._is_transient(error) or attempt == self.max_attempts - 1:
                    raise
                self.sleep(self._retry_delay(error, attempt))

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        response = getattr(error, "resp", None)
        status = getattr(response, "status", None) or getattr(error, "status_code", None)
        if status is not None:
            if int(status) == 429 or 500 <= int(status) <= 599:
                return True
            if int(status) == 403:
                try:
                    content = getattr(error, "content", b"{}")
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    errors = json.loads(content).get("error", {}).get("errors", [])
                    return any(item.get("reason") == "rateLimitExceeded" or item.get("domain") == "usageLimits" for item in errors)
                except (TypeError, ValueError, UnicodeDecodeError):
                    return False
            return False
        return isinstance(error, (TimeoutError, ConnectionError, OSError, *GOOGLE_TRANSPORT_ERRORS))

    @staticmethod
    def _retry_delay(error: Exception, attempt: int) -> float:
        response = getattr(error, "resp", None)
        headers = getattr(response, "headers", response)
        retry_after = (headers.get("Retry-After") or headers.get("retry-after")) if headers is not None else None
        try:
            return max(0.0, float(retry_after)) if retry_after is not None else min(2**attempt, 32) + random.random()
        except (TypeError, ValueError):
            return min(2**attempt, 32) + random.random()
