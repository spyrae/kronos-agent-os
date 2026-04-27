"""Import sample memories into Mem0.

Parses MEMORY.md sections and imports each fact as a separate memory.

Usage:
    python scripts/migrate_memories.py [--dry-run]
"""

import argparse
import os
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from kronos.memory.store import add_memories, get_all_memories, search_memories  # noqa: E402

USER_ID = os.environ.get("KAOS_USER_ID", "")

# Safe sample facts for testing memory import.
MEMORY_FACTS = [
    "User is building a self-hosted AI agent workspace",
    "User prefers direct, concise communication",
    "User works with Python, TypeScript, and cloud infrastructure",
    "User wants technical terms in English when useful",
    "Preferred answer format: concise, concrete, with clear next steps",
]


def migrate(dry_run: bool = False) -> None:
    """Import facts into Mem0."""
    print(f"Migrating {len(MEMORY_FACTS)} facts for user {USER_ID}")

    if not dry_run:
        existing = get_all_memories(USER_ID)
        print(f"Existing memories: {len(existing)}")

    for i, fact in enumerate(MEMORY_FACTS, 1):
        print(f"  [{i}/{len(MEMORY_FACTS)}] {fact[:80]}")
        if not dry_run:
            messages = [
                {"role": "user", "content": fact},
                {"role": "assistant", "content": "Запомнил."},
            ]
            add_memories(messages, user_id=USER_ID)

    if not dry_run:
        final = get_all_memories(USER_ID)
        print(f"\nDone. Total memories after migration: {len(final)}")

        # Verify with a search
        results = search_memories("What is the user building?", user_id=USER_ID, limit=3)
        print(f"Verification search 'What is the user building?': {results}")
    else:
        print("\n[DRY RUN] No changes made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import sample memories to Mem0")
    parser.add_argument("--dry-run", action="store_true", help="Print facts without importing")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
