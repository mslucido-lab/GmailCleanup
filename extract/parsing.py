"""Pure parsing helpers for Gmail metadata responses."""

from __future__ import annotations

from datetime import timezone
from email.utils import getaddresses, parsedate_to_datetime, parseaddr
from typing import Any, Iterable


def headers_by_name(message: dict[str, Any]) -> dict[str, list[str]]:
    headers = message.get("payload", {}).get("headers", [])
    result: dict[str, list[str]] = {}
    for header in headers:
        name = str(header.get("name", "")).lower()
        value = str(header.get("value", ""))
        if name:
            result.setdefault(name, []).append(value)
    return result


def normalized_address(value: str) -> str | None:
    """Return one lower-cased mailbox address, or None for malformed input."""
    _, address = parseaddr(value)
    address = address.strip().lower()
    return address if address.count("@") == 1 else None


def recipient_addresses(values: Iterable[str]) -> set[str]:
    """Parse every recipient in a collection of To/Cc headers."""
    return {
        address.lower()
        for _, address in getaddresses(values)
        if address and address.count("@") == 1
    }


def message_timestamp(headers: dict[str, list[str]], message: dict[str, Any]) -> int:
    """Return the Date-header epoch, falling back to Gmail's internal date.

    The Date header is the contract's primary value.  The fallback keeps a
    malformed legacy header from aborting a 400k-message metadata pull.
    """
    for raw_date in headers.get("date", []):
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    return int(message.get("internalDate", 0)) // 1000

