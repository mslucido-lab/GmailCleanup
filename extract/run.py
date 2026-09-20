"""Command-line entry point for the metadata-only Gmail pull."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from db import connect, migrate

from .gateway import GoogleGmailGateway
from .runner import Extractor


METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


def gmail_service(credentials_path: Path, token_path: Path):
    """Authorize only Gmail metadata access, importing Google libraries lazily."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError(
            "Install Google API dependencies before running extraction: "
            "pip install -r requirements.txt"
        ) from error

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(token_path, [METADATA_SCOPE])
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, [METADATA_SCOPE])
        credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def load_settings(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("Install project dependencies: pip install -r requirements.txt") from error
    with path.open(encoding="utf-8") as stream:
        settings = yaml.safe_load(stream) or {}
    if not isinstance(settings.get("PROTECTED_LABELS", []), list):
        raise ValueError("PROTECTED_LABELS must be a YAML list")
    return settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Gmail metadata into the local cleanup database.")
    parser.add_argument("--database", type=Path, default=Path("data/gmail_cleanup.db"))
    parser.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    parser.add_argument("--token", type=Path, default=Path("token.json"))
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument(
        "--protected-label",
        action="append",
        dest="protected_labels",
        default=None,
        help="Mark-owned protected label to resolve; repeatable (default: STARRED, IMPORTANT).",
    )
    args = parser.parse_args()
    settings = load_settings(args.settings)
    protected_labels = args.protected_labels or settings["PROTECTED_LABELS"]

    connection = connect(args.database)
    try:
        migrate(connection)
        extractor = Extractor(
            connection,
            GoogleGmailGateway(gmail_service(args.credentials, args.token)),
            batch_interval_seconds=float(settings.get("GMAIL_METADATA_BATCH_INTERVAL_SECONDS", 3.1)),
        )
        extractor.resolve_protected_labels(protected_labels, now=int(time.time()))
        messages = extractor.extract_messages()
        recipients = extractor.extract_sent_recipients()
    finally:
        connection.close()
    print(f"Extracted/updated {messages} messages and {recipients} Sent recipients.")


if __name__ == "__main__":
    main()
