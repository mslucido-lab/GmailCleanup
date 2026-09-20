from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from db import connect, migrate
from extract.runner import Extractor, MAIN_HEADERS, SENT_HEADERS


class FakeGateway:
    def __init__(self, pages: dict[tuple[str | None, tuple[str, ...] | None], dict[str, Any]], messages: dict[str, dict[str, Any]]) -> None:
        self.pages = pages
        self.messages = messages
        self.header_requests: list[tuple[str, ...]] = []

    def list_messages(self, *, page_token: str | None, label_ids: Sequence[str] | None = None) -> dict[str, Any]:
        return self.pages[(page_token, tuple(label_ids) if label_ids else None)]

    def get_messages(self, ids: Sequence[str], metadata_headers: Sequence[str]) -> list[dict[str, Any]]:
        self.header_requests.append(tuple(metadata_headers))
        return [self.messages[message_id] for message_id in ids]

    def list_labels(self) -> list[dict[str, str]]:
        return [{"name": "STARRED", "id": "STARRED"}, {"name": "Family", "id": "Label_9"}]


def metadata(message_id: str, headers: list[tuple[str, str]], labels: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": "thread-" + message_id,
        "internalDate": "1700000000000",
        "sizeEstimate": 123,
        "labelIds": labels or [],
        "payload": {"headers": [{"name": name, "value": value} for name, value in headers]},
    }


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temp_dir.name) / "test.db")
        migrate(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_main_pass_stores_metadata_and_checkpoint_then_does_not_restart(self) -> None:
        messages = {
            "m1": metadata("m1", [
                ("From", "Store <offers@shop.example>"),
                ("Subject", "Save today"),
                ("Date", "Tue, 14 Nov 2023 22:13:20 +0000"),
                ("List-Unsubscribe", "<https://example.test/unsubscribe>"),
            ], ["UNREAD", "STARRED"]),
            "m2": metadata("m2", [
                ("From", "person@example.net"),
                ("Subject", "Hello"),
                ("Date", "Wed, 15 Nov 2023 10:00:00 +0000"),
            ]),
        }
        gateway = FakeGateway(
            {
                (None, None): {"messages": [{"id": "m1"}], "nextPageToken": "page-2"},
                ("page-2", None): {"messages": [{"id": "m2"}]},
            },
            messages,
        )
        extractor = Extractor(self.connection, gateway)

        self.assertEqual(extractor.extract_messages(), 2)
        self.assertEqual(extractor.extract_messages(), 0)
        stored = self.connection.execute(
            "SELECT sender_email, sender_domain, is_read, is_starred, has_list_unsubscribe FROM messages WHERE message_id = 'm1'"
        ).fetchone()
        self.assertEqual(tuple(stored), ("offers@shop.example", "shop.example", 0, 1, 1))
        self.assertEqual(self.connection.execute("SELECT last_page_token, message_count FROM run_state").fetchone()[0], None)
        self.assertTrue(all(request == MAIN_HEADERS for request in gateway.header_requests))

    def test_sent_pass_persists_lowercased_to_and_cc_addresses_idempotently(self) -> None:
        gateway = FakeGateway(
            {(None, ("SENT",)): {"messages": [{"id": "sent-1"}]}},
            {"sent-1": metadata("sent-1", [
                ("To", "Alice <ALICE@example.com>, bob@example.com"),
                ("Cc", "Carol <carol@Example.com>"),
            ])},
        )
        extractor = Extractor(self.connection, gateway)
        self.assertEqual(extractor.extract_sent_recipients(), 3)
        self.assertEqual(extractor.extract_sent_recipients(), 0)
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT recipient_email FROM sent_recipients ORDER BY recipient_email")],
            ["alice@example.com", "bob@example.com", "carol@example.com"],
        )
        self.assertTrue(all(request == SENT_HEADERS for request in gateway.header_requests))

    def test_protected_labels_fail_closed_before_writing_label_map(self) -> None:
        gateway = FakeGateway({}, {})
        extractor = Extractor(self.connection, gateway)
        with self.assertRaisesRegex(ValueError, "Missing"):
            extractor.resolve_protected_labels(["Missing"], now=1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM label_map").fetchone()[0], 0)

    def test_malformed_from_is_logged_and_skipped_without_aborting_the_page(self) -> None:
        gateway = FakeGateway(
            {(None, None): {"messages": [{"id": "bad"}, {"id": "good"}]}},
            {
                "bad": metadata("bad", [("From", "not an address"), ("Date", "Tue, 14 Nov 2023 22:13:20 +0000")]),
                "good": metadata("good", [("From", "good@example.com"), ("Date", "Tue, 14 Nov 2023 22:13:20 +0000")]),
            },
        )
        with self.assertLogs("extract.runner", level="WARNING") as logs:
            self.assertEqual(Extractor(self.connection, gateway).extract_messages(), 1)
        self.assertIn("Skipping message bad", logs.output[0])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_retry_only_retries_transient_errors_and_honors_retry_after(self) -> None:
        class Response(dict):
            status = 429

        class RateLimited(Exception):
            resp = Response({"Retry-After": "3"})

        gateway = FakeGateway({}, {})
        delays: list[float] = []
        extractor = Extractor(self.connection, gateway, max_attempts=2, sleep=delays.append)
        calls = 0

        def rate_limited_once() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimited()
            return "ok"

        self.assertEqual(extractor._request(rate_limited_once), "ok")
        self.assertEqual(delays, [3.0])

        with self.assertRaises(ValueError):
            extractor._request(lambda: (_ for _ in ()).throw(ValueError("bug")))

    def test_quota_style_403_is_retryable_but_other_403_is_not(self) -> None:
        class Response:
            status = 403

        class QuotaError(Exception):
            resp = Response()
            content = b'{"error":{"errors":[{"reason":"rateLimitExceeded","domain":"usageLimits"}]}}'

        class ForbiddenError(Exception):
            resp = Response()
            content = b'{"error":{"errors":[{"reason":"forbidden","domain":"global"}]}}'

        self.assertTrue(Extractor._is_transient(QuotaError()))
        self.assertFalse(Extractor._is_transient(ForbiddenError()))

    def test_metadata_batches_are_proactively_paced(self) -> None:
        gateway = FakeGateway({(None, None): {"messages": [{"id": "m1"}]}}, {"m1": metadata("m1", [("From", "sender@example.com")])})
        delays: list[float] = []
        Extractor(self.connection, gateway, sleep=delays.append, batch_interval_seconds=1.1).extract_messages()
        self.assertEqual(delays, [1.1])


if __name__ == "__main__":
    unittest.main()
