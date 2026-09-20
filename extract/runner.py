"""Resumable, metadata-only extraction into the shared SQLite contract."""

from __future__ import annotations

import sqlite3
import json
import logging
import random
import time
from collections.abc import Iterable, Sequence
from itertools import islice
from typing import Any

from .gateway import GmailGateway
from .parsing import headers_by_name, message_timestamp, normalized_address, recipient_addresses


MAIN_HEADERS = ("From", "Subject", "Date", "List-Unsubscribe", "List-ID")
SENT_HEADERS = ("To", "Cc")
HTTP_BATCH_SIZE = 50
LOGGER = logging.getLogger(__name__)


def chunks(values: Sequence[str], size: int = HTTP_BATCH_SIZE) -> Iterable[Sequence[str]]:
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


class Extractor:
    def __init__(
        self,
        connection: sqlite3.Connection,
        gateway: GmailGateway,
        *,
        max_attempts: int = 5,
        sleep: Any = time.sleep,
        batch_interval_seconds: float = 3.1,
    ) -> None:
        self.connection = connection
        self.gateway = gateway
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.batch_interval_seconds = batch_interval_seconds

    def _get_metadata(self, ids: Sequence[str], headers: Sequence[str]) -> list[dict[str, Any]]:
        result = self._request(lambda: self.gateway.get_messages(ids, headers))
        # HTTP batches contain many individual messages.get calls; space them
        # proactively to stay below Gmail's sustained per-user quota window.
        if self.batch_interval_seconds > 0:
            self.sleep(self.batch_interval_seconds)
        return result

    def _request(self, operation: Any) -> Any:
        """Retry transient Gmail failures; leave the page checkpoint unchanged on failure."""
        for attempt in range(self.max_attempts):
            try:
                return operation()
            except Exception as error:
                if not self._is_transient(error) or attempt == self.max_attempts - 1:
                    raise
                self.sleep(self._retry_delay(error, attempt))

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        """Retry only HTTP rate/server failures and transport-level failures."""
        response = getattr(error, "resp", None)
        status = getattr(response, "status", None) or getattr(error, "status_code", None)
        if status is not None:
            if status == 429 or 500 <= int(status) <= 599:
                return True
            if int(status) == 403:
                try:
                    content = getattr(error, "content", b"{}")
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    details = json.loads(content).get("error", {}).get("errors", [])
                    return any(
                        item.get("reason") == "rateLimitExceeded" or item.get("domain") == "usageLimits"
                        for item in details
                    )
                except (TypeError, ValueError, UnicodeDecodeError):
                    return False
            return False
        return isinstance(error, (TimeoutError, ConnectionError, OSError))

    @staticmethod
    def _retry_delay(error: Exception, attempt: int) -> float:
        response = getattr(error, "resp", None)
        headers = getattr(response, "headers", response)
        if headers is not None:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            if retry_after is not None:
                try:
                    return max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    pass
        return min(2**attempt, 32) + random.random()

    def resolve_protected_labels(self, protected_label_names: Sequence[str], *, now: int) -> None:
        """Resolve Mark-owned protected labels, failing closed on any miss."""
        available = {label["name"]: label["id"] for label in self._request(self.gateway.list_labels)}
        missing = [name for name in protected_label_names if name not in available]
        if missing:
            raise ValueError("Configured protected labels do not exist: " + ", ".join(missing))
        with self.connection:
            for name in protected_label_names:
                self.connection.execute(
                    """
                    INSERT INTO label_map (label_name, label_id, resolved_at) VALUES (?, ?, ?)
                    ON CONFLICT(label_name) DO UPDATE SET
                        label_id = excluded.label_id,
                        resolved_at = excluded.resolved_at
                    """,
                    (name, available[name], now),
                )

    def extract_messages(self) -> int:
        """Resume the main mailbox pass. Returns the number of rows written."""
        checkpoint = self.connection.execute(
            "SELECT last_page_token, message_count FROM run_state WHERE id = 1"
        ).fetchone()
        if checkpoint is not None and checkpoint["last_page_token"] is None:
            return 0  # A stored NULL token means a prior run completed.

        page_token = checkpoint["last_page_token"] if checkpoint else None
        total = checkpoint["message_count"] if checkpoint else 0
        written = 0
        while True:
            page = self._request(lambda: self.gateway.list_messages(page_token=page_token))
            ids = [message["id"] for message in page.get("messages", [])]
            for message_ids in chunks(ids):
                metadata = self._get_metadata(message_ids, MAIN_HEADERS)
                written += self._upsert_messages(metadata)

            next_token = page.get("nextPageToken")
            total += len(ids)
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO run_state (id, last_page_token, message_count, updated_at)
                    VALUES (1, ?, ?, unixepoch())
                    ON CONFLICT(id) DO UPDATE SET
                        last_page_token = excluded.last_page_token,
                        message_count = excluded.message_count,
                        updated_at = excluded.updated_at
                    """,
                    (next_token, total),
                )
            if next_token is None:
                return written
            page_token = next_token

    def extract_sent_recipients(self) -> int:
        """Rebuild the idempotent Sent-folder set without a separate checkpoint."""
        page_token: str | None = None
        inserted = 0
        while True:
            page = self._request(
                lambda: self.gateway.list_messages(page_token=page_token, label_ids=("SENT",))
            )
            ids = [message["id"] for message in page.get("messages", [])]
            for message_ids in chunks(ids):
                for message in self._get_metadata(message_ids, SENT_HEADERS):
                    headers = headers_by_name(message)
                    addresses = recipient_addresses(headers.get("to", []) + headers.get("cc", []))
                    with self.connection:
                        for address in addresses:
                            result = self.connection.execute(
                                "INSERT OR IGNORE INTO sent_recipients (recipient_email) VALUES (?)",
                                (address,),
                            )
                            inserted += result.rowcount
            page_token = page.get("nextPageToken")
            if page_token is None:
                return inserted

    def _upsert_messages(self, messages: Sequence[dict[str, Any]]) -> int:
        rows: list[tuple[Any, ...]] = []
        for message in messages:
            headers = headers_by_name(message)
            sender = normalized_address((headers.get("from") or [""])[0])
            if sender is None:
                LOGGER.warning("Skipping message %s: unparseable From header", message.get("id"))
                continue
            labels = message.get("labelIds", [])
            rows.append((
                message["id"],
                message.get("threadId", message["id"]),
                sender,
                sender.rsplit("@", 1)[1],
                (headers.get("subject") or [""])[0],
                message_timestamp(headers, message),
                int(message.get("sizeEstimate", 0)),
                int("UNREAD" not in labels),
                int("STARRED" in labels),
                int(bool(headers.get("list-unsubscribe") or headers.get("list-id"))),
                json.dumps(labels),
            ))
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO messages (
                    message_id, thread_id, sender_email, sender_domain, subject, date,
                    size_bytes, is_read, is_starred, has_list_unsubscribe, labels
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    sender_email = excluded.sender_email,
                    sender_domain = excluded.sender_domain,
                    subject = excluded.subject,
                    date = excluded.date,
                    size_bytes = excluded.size_bytes,
                    is_read = excluded.is_read,
                    is_starred = excluded.is_starred,
                    has_list_unsubscribe = excluded.has_list_unsubscribe,
                    labels = excluded.labels
                """,
                rows,
            )
        return len(rows)
