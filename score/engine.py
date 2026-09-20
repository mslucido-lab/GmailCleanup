from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable
from typing import Any, Protocol


CATEGORY_WEIGHT = {
    "Marketing / promotional": 35,
    "Automated notifications": 35,
    "Newsletters / subscriptions": 25,
    "Transactional / receipts": 15,
    "Business-critical": 0,
    "Personal correspondence": 0,
}
TRANSACTIONAL = re.compile(r"order|receipt|invoice|billing|statement|shipping|confirmation", re.I)
NOTIFICATION = re.compile(r"notify|notification|alert|no-?reply|do-?not-?reply|system|updates", re.I)
PROMOTIONAL = re.compile(r"sale|save|deal|offer|discount|% off|free shipping", re.I)


@dataclass(frozen=True)
class Identity:
    email: str
    domain: str
    category: str
    source: str
    rationale: str
    brand_source: str
    inferred_brand: str | None = None
    is_esp_routed: int | None = None


class Classifier(Protocol):
    def classify(self, senders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]: ...


def classify(
    email: str,
    domain: str,
    subjects: Iterable[str],
    has_list_unsubscribe: bool,
    sent_recipients: set[str],
    allowlist: set[str],
    consumer_domains: set[str],
    known_esp_domains: set[str] | None = None,
) -> Identity:
    """Apply deterministic Stage 1; ambiguity deliberately falls safe."""
    email = email.lower()
    domain = domain.lower()
    subject_text = "\n".join(subjects)
    local_part = email.rsplit("@", 1)[0]
    if email in sent_recipients:
        category, why = "Personal correspondence", "You have sent mail to this address."
    elif email in allowlist or domain in allowlist:
        category, why = "Business-critical", "Matches the business-critical allow-list."
    elif TRANSACTIONAL.search(local_part) or TRANSACTIONAL.search(subject_text):
        category, why = "Transactional / receipts", "Address or subject matches a transactional pattern."
    elif NOTIFICATION.search(local_part) and not has_list_unsubscribe:
        category, why = "Automated notifications", "Automated sender pattern without unsubscribe headers."
    elif has_list_unsubscribe and (PROMOTIONAL.search(subject_text) or domain in (known_esp_domains or set())):
        category, why = "Marketing / promotional", "Unsubscribe header and promotional subject pattern."
    elif has_list_unsubscribe:
        category, why = "Newsletters / subscriptions", "List-Unsubscribe or List-ID header is present."
    else:
        category, why = "Personal correspondence", "Ambiguous sender; kept in the safe fallback category."
    return Identity(
        email, domain, category,
        "rule" if category != "Personal correspondence" or email in sent_recipients else "fallback",
        why,
        "not_applicable" if domain in consumer_domains else "skipped_offline",
    )


class ScoreEngine:
    def __init__(self, connection: sqlite3.Connection, settings: dict, allowlist: Iterable[str] = ()) -> None:
        self.connection = connection
        self.consumer_domains = {item.lower() for item in settings.get("CONSUMER_DOMAINS", [])}
        self.known_esp_domains = {item.lower() for item in settings.get("KNOWN_ESP_DOMAINS", [])}
        self.esp_discovery_threshold = int(settings.get("ESP_DISCOVERY_THRESHOLD", 5))
        self.allowlist = {item.lower() for item in allowlist}

    def categorize(self, *, reclassify: bool = False, llm: Classifier | None = None) -> int:
        rows = self.connection.execute(
            "SELECT sender_email, sender_domain, subject, has_list_unsubscribe FROM messages ORDER BY date DESC"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[row["sender_email"].lower()].append(row)
        sent = {row[0].lower() for row in self.connection.execute("SELECT recipient_email FROM sent_recipients")}
        existing = {row[0] for row in self.connection.execute("SELECT sender_email FROM sender_identity")}
        identities: list[Identity] = []
        samples: dict[str, dict[str, Any]] = {}
        for email, messages in grouped.items():
            if email in existing and not reclassify:
                continue
            identity = classify(
                email, messages[0]["sender_domain"],
                (message["subject"] for message in messages[:20]),
                any(message["has_list_unsubscribe"] for message in messages),
                sent, self.allowlist, self.consumer_domains,
                self.known_esp_domains,
            )
            identities.append(identity)
            if llm and identity.domain not in self.consumer_domains:
                samples[email] = {"email": email, "domain": identity.domain, "subjects": [m["subject"] for m in messages[:20]], "has_list_unsubscribe": any(m["has_list_unsubscribe"] for m in messages), "two_way": email in sent}
        if llm and samples:
            proposed = llm.classify(list(samples.values()))
            enriched: list[Identity] = []
            for identity in identities:
                value = proposed.get(identity.email, {})
                category = value.get("category") if identity.source == "fallback" else identity.category
                if category not in CATEGORY_WEIGHT:
                    category, source = identity.category, identity.source
                else:
                    source = "llm" if identity.source == "fallback" else identity.source
                brand = value.get("inferred_brand") if isinstance(value.get("inferred_brand"), str) else None
                esp = value.get("is_esp_routed")
                brand_source = "llm" if identity.email in samples else identity.brand_source
                enriched.append(Identity(identity.email, identity.domain, category, source, str(value.get("rationale") or identity.rationale), brand_source, brand, int(esp) if isinstance(esp, bool) else None))
            identities = enriched
        now = int(time.time())
        with self.connection:
            for identity in identities:
                self.connection.execute(
                    """INSERT INTO sender_identity
                    (sender_email,sender_domain,category,category_source,inferred_brand,is_esp_routed,brand_source,rationale,model_version,classified_at)
                    VALUES (?,?,?,?,?,?,?, ?,NULL,?)
                    ON CONFLICT(sender_email) DO UPDATE SET sender_domain=excluded.sender_domain,category=excluded.category,
                    category_source=excluded.category_source,inferred_brand=excluded.inferred_brand,is_esp_routed=excluded.is_esp_routed,
                    brand_source=excluded.brand_source,rationale=excluded.rationale,classified_at=excluded.classified_at""",
                    (identity.email, identity.domain, identity.category, identity.source, identity.inferred_brand, identity.is_esp_routed, identity.brand_source, identity.rationale, now),
                )
                self.connection.execute("UPDATE messages SET category=? WHERE lower(sender_email)=?", (identity.category, identity.email))
        return len(identities)

    def rebuild_groups(self, *, today: int | None = None) -> int:
        today = today or int(time.time())
        protected_ids = {row[0] for row in self.connection.execute("SELECT label_id FROM label_map")}
        rows = self.connection.execute(
            """SELECT m.*, i.category AS identity_category, i.brand_source, i.inferred_brand, i.is_esp_routed FROM messages m
               JOIN sender_identity i ON lower(m.sender_email)=i.sender_email"""
        ).fetchall()
        by_group: dict[str, list[sqlite3.Row]] = defaultdict(list)
        brands_by_domain: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            if row["inferred_brand"]:
                brands_by_domain[row["sender_domain"].lower()].add(row["inferred_brand"].lower())
        discovered_esp = {
            domain for domain, brands in brands_by_domain.items()
            if len(brands) > self.esp_discovery_threshold
        }
        for row in rows:
            email, domain = row["sender_email"].lower(), row["sender_domain"].lower()
            if domain in self.consumer_domains:
                key = f"address:{email}"
            elif (domain in self.known_esp_domains or domain in discovered_esp or row["is_esp_routed"]) and row["inferred_brand"]:
                slug = re.sub(r"[^a-z0-9]+", "-", row["inferred_brand"].lower()).strip("-")
                key = f"brand:{slug}"
            else:
                key = f"domain:{domain}"
            by_group[key].append(row)
        with self.connection:
            pending = [row[0] for row in self.connection.execute("SELECT group_key FROM sender_groups WHERE approval_status='pending'")]
            if pending:
                self.connection.executemany("DELETE FROM group_members WHERE group_key=?", [(key,) for key in pending])
                self.connection.executemany("DELETE FROM sender_groups WHERE group_key=?", [(key,) for key in pending])
            for key, members in by_group.items():
                if self.connection.execute("SELECT 1 FROM sender_groups WHERE group_key=?", (key,)).fetchone():
                    continue
                categories = [member["identity_category"] for member in members]
                category = min(categories, key=lambda value: CATEGORY_WEIGHT[value])
                labels = [set(json.loads(member["labels"])) for member in members]
                protected = any("STARRED" in value or value & protected_ids for value in labels)
                count = len(members)
                total_size = sum(member["size_bytes"] for member in members)
                avg_date = int(sum(member["date"] for member in members) / count)
                pct_unread = sum(not member["is_read"] for member in members) / count
                pct_starred = sum(member["is_starred"] for member in members) / count
                score = self._score(category, count, members, total_size, avg_date, pct_starred, protected, today)
                self.connection.execute(
                    """INSERT INTO sender_groups (group_key,group_type,domains,category,message_count,total_size_bytes,first_seen,last_seen,avg_date,pct_unread,pct_starred,has_protected_label,delete_safety_score)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, key.split(":", 1)[0], json.dumps(sorted({member["sender_domain"] for member in members})), category,
                     count, total_size, min(member["date"] for member in members), max(member["date"] for member in members), avg_date,
                     pct_unread, pct_starred, int(protected), score),
                )
                self.connection.executemany(
                    "INSERT INTO group_members (group_key,sender_email) VALUES (?,?)",
                    [(key, email) for email in sorted({member["sender_email"].lower() for member in members})],
                )
        return len(by_group)

    @staticmethod
    def _score(category: str, count: int, members: list[sqlite3.Row], total_size: int, avg_date: int, pct_starred: float, protected: bool, today: int) -> float:
        if pct_starred or protected:
            return 0.0
        pattern = min(count / 5000, 1) * 12 + (8 if today - max(row["date"] for row in members) > 365 * 86400 else 0)
        age = min(((today - avg_date) / 86400 / 365) / 6, 1) * 20
        size = min(total_size / (2 * 1024**3), 1) * 10
        return max(0.0, min(100.0, CATEGORY_WEIGHT[category] + pattern + age + size))
