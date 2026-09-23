"""CLI entry point for Gmail modify-scope batch execution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # Supports `python execute/run.py` from the spec.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import connect, migrate
from execute.gateway import GoogleGmailModifyGateway
from execute.runner import Executor


MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def gmail_service(credentials_path: Path, token_path: Path):
    """Authorize execution separately from metadata-only extraction."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError("Install Google API dependencies: pip install -r requirements.txt") from error

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(token_path, [MODIFY_SCOPE])
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, [MODIFY_SCOPE])
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
    parser = argparse.ArgumentParser(description="Preflight-gated Gmail Cleanup execution.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--archive", metavar="BATCH_ID")
    action.add_argument("--restore", metavar="BATCH_ID")
    action.add_argument("--trash", metavar="BATCH_ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the planned work without modifying Gmail or SQLite (default).")
    mode.add_argument("--live", action="store_true", help="Perform Gmail and execution-state writes.")
    parser.add_argument("--database", type=Path, default=Path("data/gmail_cleanup.db"))
    parser.add_argument("--credentials", type=Path, default=Path("credentials.json"))
    parser.add_argument("--token", type=Path, default=Path("data/execute_token.json"))
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    args = parser.parse_args()

    settings = load_settings(args.settings)
    connection = connect(args.database)
    try:
        migrate(connection)
        executor = Executor(
            connection,
            GoogleGmailModifyGateway(gmail_service(args.credentials, args.token)),
            protected_label_names=settings["PROTECTED_LABELS"],
            restore_window_days=int(settings.get("RESTORE_WINDOW_DAYS", 30)),
            batch_size_cap=int(settings.get("BATCH_SIZE_CAP", 5_000)),
            gmail_batch_size=int(settings.get("GMAIL_METADATA_BATCH_SIZE", 10)),
            gmail_batch_interval_seconds=float(settings.get("GMAIL_METADATA_BATCH_INTERVAL_SECONDS", 2.0)),
        )
        executor.provision_labels(allow_create=args.live)
        batch_id, operation = next((value, name) for name, value in (("archive", args.archive), ("restore", args.restore), ("trash", args.trash)) if value)
        result = getattr(executor, operation)(batch_id, dry_run=not args.live)
    finally:
        connection.close()
    print(("Executed" if args.live else "Dry run") + f" {operation} for {batch_id}: {result}")


if __name__ == "__main__":
    main()
