"""Opt-in Anthropic metadata classifier; never invoked unless explicitly enabled."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


VALID_CATEGORIES = {
    "Marketing / promotional", "Automated notifications", "Newsletters / subscriptions",
    "Transactional / receipts", "Business-critical", "Personal correspondence",
}


class AnthropicClassifier:
    BATCH_SIZE = 25
    def __init__(self, model: str, api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ENABLE_LLM_CLASSIFICATION requires ANTHROPIC_API_KEY")

    def classify(self, senders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(senders), self.BATCH_SIZE):
            results.update(self._classify_batch(senders[offset:offset + self.BATCH_SIZE]))
        return results

    def _classify_batch(self, senders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        prompt = (
            "Classify each sender. Return JSON only: an array with email, category, inferred_brand, "
            "is_esp_routed, rationale. category must be one of: " + ", ".join(sorted(VALID_CATEGORIES)) +
            ". Do not infer bodies; use only this metadata.\n" + json.dumps(senders)
        )
        max_tokens = min(8192, max(1024, 320 * len(senders) + 256))
        payload = json.dumps({"model": self.model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}).encode()
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", payload,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read())
        text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
        values = json.loads(self._json_text(text))
        if not isinstance(values, list):
            raise ValueError("Anthropic classification response must be a JSON array")
        return {item["email"].lower(): item for item in values if isinstance(item, dict) and isinstance(item.get("email"), str)}

    @staticmethod
    def _json_text(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()
