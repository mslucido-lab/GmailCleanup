from __future__ import annotations

import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from db import connect, migrate


APP_PATH = Path(__file__).parents[1] / "review-ui" / "app.py"
SPEC = importlib.util.spec_from_file_location("review_ui_app", APP_PATH)
review_ui = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(review_ui)


class ReviewUiApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "review.db"
        connection = connect(self.path)
        migrate(connection)
        with connection:
            connection.execute("INSERT INTO sender_identity VALUES ('offers@example.com','example.com','Marketing / promotional','rule',NULL,NULL,'skipped_offline','promo',NULL,1)")
            connection.execute("INSERT INTO messages (message_id,thread_id,sender_email,sender_domain,subject,date,size_bytes,is_read,is_starred,has_list_unsubscribe,labels,category) VALUES ('m1','t1','offers@example.com','example.com','Sale',1,100,1,0,1,'[]','Marketing / promotional')")
            connection.execute("INSERT INTO sender_groups (group_key,group_type,domains,category,message_count,total_size_bytes,first_seen,last_seen,avg_date,pct_unread,pct_starred,has_protected_label,delete_safety_score) VALUES ('domain:example.com','domain','[\"example.com\"]','Marketing / promotional',1,100,1,1,1,0,0,0,50)")
            connection.execute("INSERT INTO group_members VALUES ('domain:example.com','offers@example.com')")
        connection.close()
        review_ui.DATABASE = self.path
        self.client = TestClient(review_ui.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_approval_creates_immutable_batch_and_audit_event(self) -> None:
        response = self.client.post("/api/groups/domain:example.com/decision", json={"status": "approved"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        connection = connect(self.path)
        self.assertEqual(connection.execute("SELECT status FROM batches WHERE batch_id=?", (body["batch_id"],)).fetchone()[0], "approved")
        self.assertEqual(connection.execute("SELECT event FROM audit_log").fetchone()[0], "approved")
        connection.close()
        self.assertEqual(self.client.post("/api/groups/domain:example.com/decision", json={"status": "approved"}).status_code, 409)
        self.assertEqual(self.client.get("/api/groups").json(), [])

    def _batch_in_restore_window(self, batch_id: str = "restore-batch", deadline: float | None = None) -> str:
        deadline = deadline if deadline is not None else time.time() + 86_400
        connection = connect(self.path)
        with connection:
            connection.execute(
                """INSERT INTO batches
                   (batch_id, group_key, status, approved_at, message_count, total_size_bytes, restore_deadline)
                   VALUES (?, 'domain:example.com', 'restore_window', unixepoch(), 1, 100, ?)""",
                (batch_id, deadline),
            )
            connection.execute(
                "INSERT INTO batch_messages (batch_id, message_id, status, original_labels) VALUES (?, 'm1', 'labeled', '[\"INBOX\"]')",
                (batch_id,),
            )
        connection.close()
        return batch_id

    def test_business_critical_group_cannot_be_approved(self) -> None:
        connection = connect(self.path)
        with connection:
            connection.execute("UPDATE sender_groups SET category='Business-critical' WHERE group_key='domain:example.com'")
        connection.close()
        response = self.client.post("/api/groups/domain:example.com/decision", json={"status": "approved"})
        self.assertEqual(response.status_code, 403)

    def test_extend_window_records_audited_extension(self) -> None:
        batch_id = self._batch_in_restore_window()
        connection = connect(self.path)
        before = connection.execute("SELECT restore_deadline FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0]
        connection.close()
        response = self.client.post(f"/api/batches/{batch_id}/extend-window", json={"days": 3})
        self.assertEqual(response.status_code, 200)
        connection = connect(self.path)
        after = connection.execute("SELECT restore_deadline, window_extensions FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        audit = connection.execute("SELECT event, note FROM audit_log WHERE batch_id=?", (batch_id,)).fetchone()
        connection.close()
        self.assertEqual(after[0], before + 3 * 86_400)
        self.assertEqual(after[1], 1)
        self.assertEqual(tuple(audit), ("window_extended", "extended +3d"))

    def test_trash_confirmation_requires_expired_window_and_records_hash(self) -> None:
        batch_id = self._batch_in_restore_window(deadline=time.time() - 1)
        original_invoke = review_ui.invoke
        review_ui.invoke = lambda *_: None
        try:
            response = self.client.post(f"/api/batches/{batch_id}/confirm-trash", json={"note": "reviewed"})
        finally:
            review_ui.invoke = original_invoke
        self.assertEqual(response.status_code, 200)
        connection = connect(self.path)
        batch = connection.execute(
            "SELECT permanent_delete_confirmed_at, confirmation_snapshot_hash FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        audit = connection.execute("SELECT event, note FROM audit_log WHERE batch_id=?", (batch_id,)).fetchone()
        connection.close()
        self.assertIsNotNone(batch[0])
        self.assertEqual(batch[1], response.json()["confirmation_snapshot_hash"])
        self.assertEqual(tuple(audit), ("permanent_delete_confirmed", "reviewed"))

    def test_trash_confirmation_rejects_an_active_restore_window(self) -> None:
        batch_id = self._batch_in_restore_window()
        response = self.client.post(f"/api/batches/{batch_id}/confirm-trash", json={"note": "too soon"})
        self.assertEqual(response.status_code, 409)

    def test_restore_and_archive_use_executor_bridge(self) -> None:
        restore_id = self._batch_in_restore_window()
        connection = connect(self.path)
        with connection:
            connection.execute(
                """INSERT INTO batches (batch_id, group_key, status, approved_at, message_count, total_size_bytes)
                   VALUES ('approved-batch', 'domain:example.com', 'approved', unixepoch(), 1, 100)"""
            )
        connection.close()
        calls: list[tuple[str, ...]] = []
        original_invoke = review_ui.invoke
        review_ui.invoke = lambda *arguments: calls.append(arguments)
        try:
            self.assertEqual(self.client.post(f"/api/batches/{restore_id}/restore").status_code, 200)
            self.assertEqual(self.client.post("/api/batches/approved-batch/start-archive").status_code, 200)
        finally:
            review_ui.invoke = original_invoke
        self.assertEqual(calls, [("--live", "--restore", restore_id), ("--live", "--archive", "approved-batch")])

    def test_failed_batch_retry_uses_pending_message_state_to_resume_archive(self) -> None:
        batch_id = self._batch_in_restore_window("failed-batch")
        connection = connect(self.path)
        with connection:
            connection.execute("UPDATE batches SET status='failed' WHERE batch_id=?", (batch_id,))
            connection.execute("UPDATE batch_messages SET status='pending' WHERE batch_id=?", (batch_id,))
        connection.close()
        calls: list[tuple[str, ...]] = []
        original_invoke = review_ui.invoke
        review_ui.invoke = lambda *arguments: calls.append(arguments)
        try:
            response = self.client.post(f"/api/batches/{batch_id}/retry", json={"operation": "archive"})
        finally:
            review_ui.invoke = original_invoke
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["operation"], "archive")
        self.assertEqual(calls, [("--live", "--archive", batch_id)])
        batches = self.client.get("/api/batches").json()
        self.assertEqual(batches[0]["retry_operations"], ["archive"])

    def test_ambiguous_failed_batch_requires_explicit_restore_or_trash_choice(self) -> None:
        batch_id = self._batch_in_restore_window("ambiguous-failed")
        connection = connect(self.path)
        with connection:
            connection.execute("UPDATE batches SET status='failed', permanent_delete_confirmed_at=1 WHERE batch_id=?", (batch_id,))
        connection.close()
        self.assertEqual(self.client.post(f"/api/batches/{batch_id}/retry", json={}).status_code, 409)
        original_invoke = review_ui.invoke
        calls: list[tuple[str, ...]] = []
        review_ui.invoke = lambda *arguments: calls.append(arguments)
        try:
            response = self.client.post(f"/api/batches/{batch_id}/retry", json={"operation": "restore"})
        finally:
            review_ui.invoke = original_invoke
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("--live", "--restore", batch_id)])

    def test_executor_unavailable_returns_service_unavailable(self) -> None:
        batch_id = self._batch_in_restore_window()
        original_invoke = review_ui.invoke
        review_ui.invoke = lambda *_: (_ for _ in ()).throw(RuntimeError("not installed"))
        try:
            response = self.client.post(f"/api/batches/{batch_id}/restore")
        finally:
            review_ui.invoke = original_invoke
        self.assertEqual(response.status_code, 503)

    def test_root_serves_actionable_local_frontend(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Start archive run", response.text)
        self.assertIn("Confirm move to Trash", response.text)


if __name__ == "__main__":
    unittest.main()
