from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from db import connect, migrate


class DatabaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "gmail_cleanup.db"
        self.connection = connect(self.path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_initial_schema_migrates_once(self) -> None:
        self.assertEqual(migrate(self.connection), [1, 2, 3])
        self.assertEqual(migrate(self.connection), [])

        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "schema_version",
                "messages",
                "sender_identity",
                "sender_groups",
                "group_members",
                "label_map",
                "run_state",
                "sent_recipients",
                "batches",
                "batch_messages",
                "audit_log",
            }.issubset(tables)
        )
        self.assertNotIn(
            "has_attachment",
            {row[1] for row in self.connection.execute("PRAGMA table_info(messages)")},
        )
        self.assertNotIn(
            "pct_attachments",
            {row[1] for row in self.connection.execute("PRAGMA table_info(sender_groups)")},
        )

    def test_sent_recipients_are_durable_and_idempotent(self) -> None:
        migrate(self.connection)
        self.connection.execute(
            "INSERT OR IGNORE INTO sent_recipients (recipient_email) VALUES (?)",
            ("recipient@example.com",),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO sent_recipients (recipient_email) VALUES (?)",
            ("recipient@example.com",),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sent_recipients").fetchone()[0],
            1,
        )

    def test_connection_enforces_foreign_keys_and_status_constraints(self) -> None:
        migrate(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO batch_messages (batch_id, message_id, status) VALUES (?, ?, ?)",
                ("missing", "missing", "pending"),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO sender_groups (
                    group_key, group_type, category, message_count, total_size_bytes,
                    first_seen, last_seen, avg_date, pct_unread, pct_starred,
                    has_protected_label, delete_safety_score, approval_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "domain:example.com", "domain", "Marketing / promotional", 0, 0,
                    0, 0, 0, 0, 0, 0, 0, "invalid",
                ),
            )

    def test_message_category_is_nullable_but_constrained_to_the_taxonomy(self) -> None:
        migrate(self.connection)
        base_values = (
            "thread-1", "sender@example.com", "example.com", "Subject", 1, 0,
            1, 0, 0, "[]",
        )
        self.connection.execute(
            """
            INSERT INTO messages (
                message_id, thread_id, sender_email, sender_domain, subject, date,
                size_bytes, is_read, is_starred, has_list_unsubscribe, labels, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("message-null-category", *base_values, None),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO messages (
                    message_id, thread_id, sender_email, sender_domain, subject, date,
                size_bytes, is_read, is_starred, has_list_unsubscribe, labels, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("message-invalid-category", *base_values, "Not a real category"),
            )

    def test_wal_and_busy_timeout_are_configured(self) -> None:
        migrate(self.connection)
        self.assertEqual(self.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(self.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5_000)


if __name__ == "__main__":
    unittest.main()
