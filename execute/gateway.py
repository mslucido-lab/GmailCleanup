"""Narrow Gmail API surface used by the safety-critical executor."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class GmailModifyGateway(Protocol):
    def get_current_labels(self, ids: Sequence[str]) -> dict[str, list[str]]: ...

    def batch_modify(
        self, ids: Sequence[str], *, add_label_ids: Sequence[str], remove_label_ids: Sequence[str]
    ) -> None: ...

    def list_labels(self) -> list[dict[str, str]]: ...

    def create_label(self, name: str) -> dict[str, str]: ...


class GoogleGmailModifyGateway:
    """gmail.modify implementation using Gmail's 50-request HTTP batches."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def get_current_labels(self, ids: Sequence[str]) -> dict[str, list[str]]:
        if len(ids) > 50:
            raise ValueError("Live preflight reads are capped at 50 messages")
        responses: dict[str, dict[str, Any]] = {}
        errors: list[BaseException] = []

        def callback(request_id: str, response: Any, exception: BaseException | None) -> None:
            if exception is not None:
                errors.append(exception)
            elif response is not None:
                responses[request_id] = response

        batch = self.service.new_batch_http_request(callback=callback)
        for message_id in ids:
            batch.add(
                self.service.users().messages().get(userId="me", id=message_id, format="minimal"),
                request_id=message_id,
            )
        batch.execute()
        if errors:
            raise errors[0]
        missing = set(ids) - responses.keys()
        if missing:
            raise RuntimeError("Gmail preflight returned no response for: " + ", ".join(sorted(missing)))
        return {message_id: list(responses[message_id].get("labelIds", [])) for message_id in ids}

    def batch_modify(
        self, ids: Sequence[str], *, add_label_ids: Sequence[str], remove_label_ids: Sequence[str]
    ) -> None:
        if not ids:
            return
        if len(ids) > 50:
            raise ValueError("Gmail writes are capped at 50 messages to match preflight")
        self.service.users().messages().batchModify(
            userId="me",
            body={
                "ids": list(ids),
                "addLabelIds": list(add_label_ids),
                "removeLabelIds": list(remove_label_ids),
            },
        ).execute()

    def list_labels(self) -> list[dict[str, str]]:
        return self.service.users().labels().list(userId="me").execute().get("labels", [])

    def create_label(self, name: str) -> dict[str, str]:
        return self.service.users().labels().create(userId="me", body={"name": name}).execute()
