from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from db import connect, migrate
from .engine import ScoreEngine
from .llm import AnthropicClassifier


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Categorize and score extracted Gmail metadata.")
    parser.add_argument("--database", type=Path, default=Path("data/gmail_cleanup.db"))
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--allowlists", type=Path, default=Path("config/allowlists.yaml"))
    parser.add_argument("--reclassify", action="store_true")
    args = parser.parse_args()
    settings, allowlists = load_yaml(args.settings), load_yaml(args.allowlists)
    llm = AnthropicClassifier(settings["ANTHROPIC_MODEL"]) if settings.get("ENABLE_LLM_CLASSIFICATION") else None
    connection = connect(args.database)
    try:
        migrate(connection)
        engine = ScoreEngine(connection, settings, allowlists.get("BUSINESS_CRITICAL_ALLOWLIST", []))
        identities = engine.categorize(reclassify=args.reclassify, llm=llm)
        groups = engine.rebuild_groups()
    finally:
        connection.close()
    print(f"Categorized {identities} senders and rebuilt {groups} pending groups.")


if __name__ == "__main__":
    main()
