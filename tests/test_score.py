from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from db import connect, migrate
from score.engine import CATEGORY_WEIGHT, ScoreEngine, classify
from score.llm import AnthropicClassifier


class ScoringRuleTests(unittest.TestCase):
    def test_llm_json_fences_are_removed_before_parsing(self) -> None:
        self.assertEqual(AnthropicClassifier._json_text("```json\n[]\n```"), "[]")

    def test_llm_classifier_batches_large_sender_sets(self) -> None:
        classifier = AnthropicClassifier("test-model", api_key="test-key")
        batch_sizes: list[int] = []
        classifier._classify_batch = lambda senders: (batch_sizes.append(len(senders)) or {})
        classifier.classify([{"email": f"sender-{index}@example.com"} for index in range(51)])
        self.assertEqual(batch_sizes, [25, 25, 1])
    def test_two_way_correspondence_wins_over_other_patterns(self) -> None:
        identity = classify(
            "Receipt@Example.com", "example.com", ["Your receipt"], True,
            {"receipt@example.com"}, set(), set(),
        )
        self.assertEqual(identity.category, "Personal correspondence")
        self.assertEqual(identity.source, "rule")

    def test_ambiguous_sender_uses_safe_fallback(self) -> None:
        identity = classify("hello@example.com", "example.com", ["Hello"], False, set(), set(), set())
        self.assertEqual(identity.category, "Personal correspondence")
        self.assertEqual(identity.source, "fallback")

    def test_list_mail_with_promo_subject_is_marketing(self) -> None:
        identity = classify("news@example.com", "example.com", ["Save 25% today"], True, set(), set(), set())
        self.assertEqual(identity.category, "Marketing / promotional")
        self.assertEqual(CATEGORY_WEIGHT[identity.category], 35)

    def test_list_mail_from_known_esp_is_marketing_without_promo_copy(self) -> None:
        identity = classify("news@send.example", "send.example", ["November newsletter"], True, set(), set(), set(), {"send.example"})
        self.assertEqual(identity.category, "Marketing / promotional")


class ScoringIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temp_dir.name) / "score.db")
        migrate(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _message(self, message_id: str, sender: str, subject: str, labels: str = "[]") -> None:
        domain = sender.rsplit("@", 1)[1]
        self.connection.execute(
            """INSERT INTO messages (message_id,thread_id,sender_email,sender_domain,subject,date,size_bytes,is_read,is_starred,has_list_unsubscribe,labels)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (message_id, "t-" + message_id, sender, domain, subject, 1_600_000_000, 1024, 1, 0, 1, labels),
        )

    def test_pipeline_persists_categories_and_keeps_approved_groups_immutable(self) -> None:
        self._message("m1", "offers@shop.example", "Save 20% today")
        self._message("m2", "offers@shop.example", "Save 10% today")
        self.connection.execute("INSERT INTO sent_recipients VALUES ('friend@gmail.com')")
        self._message("m3", "friend@gmail.com", "Hello")
        engine = ScoreEngine(self.connection, {"CONSUMER_DOMAINS": ["gmail.com"]})
        self.assertEqual(engine.categorize(), 2)
        self.assertEqual(engine.rebuild_groups(today=1_800_000_000), 2)
        group = self.connection.execute("SELECT category, message_count, delete_safety_score FROM sender_groups WHERE group_key='domain:shop.example'").fetchone()
        self.assertEqual(tuple(group[:2]), ("Marketing / promotional", 2))
        self.assertGreater(group[2], 35)
        self.connection.execute("UPDATE sender_groups SET approval_status='approved' WHERE group_key='domain:shop.example'")
        self.assertEqual(engine.rebuild_groups(today=1_800_000_000), 2)
        self.assertEqual(self.connection.execute("SELECT approval_status FROM sender_groups WHERE group_key='domain:shop.example'").fetchone()[0], "approved")

    def test_llm_esp_signal_creates_a_brand_group(self) -> None:
        self._message("m1", "news@mailer.example", "November update")
        class FakeLlm:
            def classify(self, senders):
                return {"news@mailer.example": {"email": "news@mailer.example", "category": "Marketing / promotional", "inferred_brand": "Example Brand", "is_esp_routed": True, "rationale": "Brand-routed mail."}}
        engine = ScoreEngine(self.connection, {"CONSUMER_DOMAINS": []})
        engine.categorize(llm=FakeLlm())
        engine.rebuild_groups(today=1_800_000_000)
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM sender_groups WHERE group_key='brand:example-brand'").fetchone())

    def test_llm_enrichment_does_not_relabel_consumer_domain_sender(self) -> None:
        self._message("work", "news@mailer.example", "Update")
        self._message("personal", "friend@gmail.com", "Hello")
        class FakeLlm:
            def classify(self, senders):
                return {"news@mailer.example": {"email": "news@mailer.example", "inferred_brand": "Mailer", "is_esp_routed": False}}
        engine = ScoreEngine(self.connection, {"CONSUMER_DOMAINS": ["gmail.com"]})
        engine.categorize(llm=FakeLlm())
        sources = dict(self.connection.execute("SELECT sender_email, brand_source FROM sender_identity"))
        self.assertEqual(sources["news@mailer.example"], "llm")
        self.assertEqual(sources["friend@gmail.com"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
