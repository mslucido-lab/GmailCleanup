from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from db import confirmation_snapshot_hash, connect, migrate
from execute.runner import Executor


class FakeGmail:
    def __init__(self, live_labels: dict[str, list[str]]) -> None:
        self.live_labels = live_labels
        self.modifies: list[tuple[list[str], list[str], list[str]]] = []
        self.fail_preflight = False
        self.fail_writes = 0

    def list_labels(self):
        return [
            {"name": "STARRED", "id": "STARRED"},
            {"name": "IMPORTANT", "id": "IMPORTANT"},
            {"name": "Cleanup/Archive", "id": "archive-label"},
        ]

    def create_label(self, name: str):
        return {"name": name, "id": "archive-label"}

    def get_current_labels(self, ids):
        if self.fail_preflight:
            raise TimeoutError("network unavailable")
        return {message_id: list(self.live_labels[message_id]) for message_id in ids}

    def batch_modify(self, ids, *, add_label_ids, remove_label_ids):
        if self.fail_writes:
            self.fail_writes -= 1
            raise TimeoutError("write failed")
        self.modifies.append((list(ids), list(add_label_ids), list(remove_label_ids)))


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "execute.db"
        self.connection = connect(self.path)
        migrate(self.connection)
        with self.connection:
            self.connection.execute(
                "INSERT INTO sender_identity VALUES ('offers@example.com','example.com','Marketing / promotional','rule',NULL,NULL,'skipped_offline','',NULL,1)"
            )
            self.connection.execute(
                """INSERT INTO sender_groups (group_key,group_type,domains,category,message_count,total_size_bytes,first_seen,last_seen,avg_date,pct_unread,pct_starred,has_protected_label,delete_safety_score)
                   VALUES ('domain:example.com','domain','[\"example.com\"]','Marketing / promotional',2,300,1,1,1,0,0,0,80)"""
            )
            self.connection.execute("INSERT INTO group_members VALUES ('domain:example.com','offers@example.com')")
            for message_id in ("m1", "m2"):
                self.connection.execute(
                    """INSERT INTO messages (message_id,thread_id,sender_email,sender_domain,subject,date,size_bytes,is_read,is_starred,has_list_unsubscribe,labels,category)
                       VALUES (?,?,'offers@example.com','example.com','sale',1,150,1,0,1,'[]','Marketing / promotional')""",
                    (message_id, message_id),
                )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _batch(self, status: str = "approved", batch_id: str = "batch") -> str:
        with self.connection:
            self.connection.execute(
                "INSERT INTO batches (batch_id,group_key,status,approved_at,message_count,total_size_bytes) VALUES (?,'domain:example.com',?,1,2,300)",
                (batch_id, status),
            )
            self.connection.executemany(
                "INSERT INTO batch_messages (batch_id,message_id,status) VALUES (?,?,'pending')",
                [(batch_id, "m1"), (batch_id, "m2")],
            )
        return batch_id

    def _executor(self, gateway: FakeGmail, **kwargs) -> Executor:
        executor = Executor(self.connection, gateway, protected_label_names=("STARRED", "IMPORTANT"), sleep=lambda _: None, **kwargs)
        executor.provision_labels(allow_create=True)
        return executor

    def test_archive_preflights_excludes_protected_and_snapshots_before_write(self) -> None:
        batch_id = self._batch()
        gateway = FakeGmail({"m1": ["INBOX"], "m2": ["INBOX", "STARRED"]})
        result = self._executor(gateway).archive(batch_id, dry_run=False)
        self.assertEqual(result, {"labeled": 1, "excluded": 1})
        self.assertEqual(gateway.modifies, [(["m1"], ["archive-label"], ["INBOX"])])
        rows = self.connection.execute("SELECT message_id,status,original_labels FROM batch_messages WHERE batch_id=? ORDER BY message_id", (batch_id,)).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("m1", "labeled"), ("m2", "excluded_protected")])
        self.assertEqual(json.loads(rows[0][2]), ["INBOX"])
        self.assertIsNone(rows[1][2])
        self.assertEqual(self.connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0], "restore_window")
        self.assertEqual(self.connection.execute("SELECT event FROM audit_log WHERE batch_id=?", (batch_id,)).fetchone()[0], "labeled")

    def test_failed_archive_keeps_original_snapshot_and_pending_status_for_resume(self) -> None:
        batch_id = self._batch()
        gateway = FakeGmail({"m1": ["INBOX"], "m2": ["INBOX"]})
        gateway.fail_writes = 1
        with self.assertRaises(TimeoutError):
            self._executor(gateway, max_attempts=1).archive(batch_id, dry_run=False)
        row = self.connection.execute("SELECT status,original_labels FROM batch_messages WHERE batch_id=? AND message_id='m1'", (batch_id,)).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(json.loads(row[1]), ["INBOX"])
        self.assertEqual(self.connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0], "failed")
        gateway.live_labels = {"m1": ["INBOX", "archive-label"], "m2": ["INBOX", "archive-label"]}
        self._executor(gateway).archive(batch_id, dry_run=False)
        snapshots = self.connection.execute("SELECT original_labels FROM batch_messages WHERE batch_id=? ORDER BY message_id", (batch_id,)).fetchall()
        self.assertEqual([json.loads(row[0]) for row in snapshots], [["INBOX"], ["INBOX"]])

    def test_restore_exactly_reapplies_original_labels(self) -> None:
        batch_id = self._batch()
        with self.connection:
            self.connection.execute("UPDATE batches SET status='restore_window', labeled_at=1, restore_deadline=? WHERE batch_id=?", (time.time() + 100, batch_id))
            self.connection.execute("UPDATE batch_messages SET status='labeled', original_labels='[\"INBOX\",\"custom\"]' WHERE batch_id=?", (batch_id,))
        gateway = FakeGmail({"m1": ["archive-label"], "m2": ["archive-label"]})
        result = self._executor(gateway).restore(batch_id, dry_run=False)
        self.assertEqual(result, {"restored": 2, "excluded": 0})
        self.assertEqual(gateway.modifies, [(["m1"], ["INBOX", "custom"], ["archive-label"]), (["m2"], ["INBOX", "custom"], ["archive-label"])])
        self.assertEqual(self.connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0], "restored")

    def test_trash_requires_matching_second_confirmation_then_uses_trash_not_delete(self) -> None:
        batch_id = self._batch()
        with self.connection:
            self.connection.execute("UPDATE batches SET status='restore_window', labeled_at=1, restore_deadline=? WHERE batch_id=?", (time.time() - 1, batch_id))
            self.connection.execute("UPDATE batch_messages SET status='labeled', original_labels='[\"INBOX\"]' WHERE batch_id=?", (batch_id,))
            digest = confirmation_snapshot_hash(self.connection, batch_id)
            self.connection.execute("UPDATE batches SET permanent_delete_confirmed_at=1, confirmation_snapshot_hash=? WHERE batch_id=?", (digest, batch_id))
        gateway = FakeGmail({"m1": ["archive-label"], "m2": ["archive-label"]})
        result = self._executor(gateway).trash(batch_id, dry_run=False)
        self.assertEqual(result, {"trashed": 2, "excluded": 0})
        self.assertEqual(gateway.modifies, [(["m1", "m2"], ["TRASH"], ["archive-label"])])
        self.assertEqual(self.connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0], "trashed")

    def test_hash_mismatch_invalidates_confirmation_without_a_gmail_write(self) -> None:
        batch_id = self._batch()
        with self.connection:
            self.connection.execute("UPDATE batches SET status='restore_window', restore_deadline=?, permanent_delete_confirmed_at=1, confirmation_snapshot_hash='bad' WHERE batch_id=?", (time.time() - 1, batch_id))
            self.connection.execute("UPDATE batch_messages SET status='labeled' WHERE batch_id=?", (batch_id,))
        gateway = FakeGmail({"m1": ["archive-label"], "m2": ["archive-label"]})
        with self.assertRaisesRegex(ValueError, "invalidated"):
            self._executor(gateway).trash(batch_id, dry_run=False)
        row = self.connection.execute("SELECT permanent_delete_confirmed_at,confirmation_snapshot_hash FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.assertEqual(gateway.modifies, [])

    def test_dry_run_does_not_change_gmail_or_execution_state(self) -> None:
        batch_id = self._batch()
        gateway = FakeGmail({"m1": ["INBOX"], "m2": ["INBOX"]})
        result = self._executor(gateway).archive(batch_id)
        self.assertEqual(result, {"planned": 2})
        self.assertEqual(gateway.modifies, [])
        self.assertEqual(self.connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0], "approved")

    def test_preflight_failure_marks_batch_failed_without_touching_rows(self) -> None:
        batch_id = self._batch()
        gateway = FakeGmail({"m1": ["INBOX"], "m2": ["INBOX"]})
        gateway.fail_preflight = True
        with self.assertRaises(TimeoutError):
            self._executor(gateway, max_attempts=1).archive(batch_id, dry_run=False)
        self.assertEqual(self.connection.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0], "failed")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM batch_messages WHERE batch_id=? AND status='pending'", (batch_id,)).fetchone()[0], 2)

    def test_starred_messages_are_protected_even_if_not_configured_as_a_label(self) -> None:
        batch_id = self._batch()
        gateway = FakeGmail({"m1": ["INBOX", "STARRED"], "m2": ["INBOX"]})
        executor = Executor(self.connection, gateway, protected_label_names=("IMPORTANT",), sleep=lambda _: None)
        executor.provision_labels(allow_create=True)
        executor.archive(batch_id, dry_run=False)
        rows = self.connection.execute("SELECT message_id,status FROM batch_messages WHERE batch_id=? ORDER BY message_id", (batch_id,)).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("m1", "excluded_protected"), ("m2", "labeled")])


if __name__ == "__main__":
    unittest.main()
