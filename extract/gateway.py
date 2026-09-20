"""Gmail API adapter.  The extractor core depends only on this small surface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class GmailGateway(Protocol):
    def list_messages(self, *, page_token: str | None, label_ids: Sequence[str] | None = None) -> dict[str, Any]: ...

    def get_messages(self, ids: Sequence[str], metadata_headers: Sequence[str]) -> list[dict[str, Any]]: ...

    def list_labels(self) -> list[dict[str, str]]: ...


class GoogleGmailGateway:
    """Adapter around google-api-python-client's HTTP multipart batching."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def list_messages(self, *, page_token: str | None, label_ids: Sequence[str] | None = None) -> dict[str, Any]:
        request = self.service.users().messages().list(
            userId="me",
            maxResults=500,
            pageToken=page_token,
            labelIds=list(label_ids) if label_ids else None,
        )
        return request.execute()

    def get_messages(self, ids: Sequence[str], metadata_headers: Sequence[str]) -> list[dict[str, Any]]:
        if len(ids) > 50:
            raise ValueError("Gmail HTTP batches are capped at 50 inner requests")
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
                self.service.users().messages().get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=list(metadata_headers),
                ),
                request_id=message_id,
            )
        batch.execute()
        if errors:
            raise errors[0]
        missing = set(ids) - responses.keys()
        if missing:
            raise RuntimeError("Gmail batch returned no response for: " + ", ".join(sorted(missing)))
        return [responses[message_id] for message_id in ids]

    def list_labels(self) -> list[dict[str, str]]:
        return self.service.users().labels().list(userId="me").execute().get("labels", [])
