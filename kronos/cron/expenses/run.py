"""Manual runner for the email-expenses pipeline.

Examples::

    # Safe preview against the live mailbox — extracts + shows, writes NOTHING:
    python -m kronos.cron.expenses.run --dry-run --stdout

    # Real run (writes to Notion, archives emails), report to the finance topic:
    python -m kronos.cron.expenses.run

    # Real run but print the report locally instead of Telegram:
    python -m kronos.cron.expenses.run --stdout

The report is identical to what the daily cron posts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from kronos.cron.expenses.processor import run_email_expenses


def _stdout_notifier(text: str, topic_id=None) -> bool:
    print("\n" + "=" * 64)
    print(text)
    print("=" * 64 + "\n")
    return True


async def _main(dry_run: bool, stdout: bool) -> None:
    kwargs: dict = {"dry_run": dry_run}
    if stdout:
        kwargs["notifier"] = _stdout_notifier
    counts = await run_email_expenses(**kwargs)
    print(f"counts: {counts}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the email-expenses pipeline once.")
    parser.add_argument(
        "--dry-run", action="store_true", help="extract and report only; do not write to Notion or archive email"
    )
    parser.add_argument(
        "--stdout", action="store_true", help="print the report to stdout instead of posting to Telegram"
    )
    parser.add_argument("--verbose", action="store_true", help="INFO-level logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main(args.dry_run, args.stdout))


if __name__ == "__main__":
    main()
