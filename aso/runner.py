#!/usr/bin/env python3
"""ASO Pipeline Runner — CLI entry point.

Usage:
    python -m aso run                  Run one full cycle
    python -m aso run --dry-run        Run without Telegram notifications
    python -m aso approve              Resume paused graph: approve plan
    python -m aso reject "comment"     Resume paused graph: reject with feedback
    python -m aso skip                 Resume paused graph: skip this cycle
    python -m aso resume               Resume after wait period
    python -m aso status               Show current pipeline status
    python -m aso history              Show recent cycle history

Environment:
    DEEPSEEK_API_KEY      Required: LLM provider key
    ASC_KEY_ID            App Store Connect API Key ID
    ASC_ISSUER_ID         App Store Connect Issuer ID
    ASC_PRIVATE_KEY       Path to .p8 private key
    WEBHOOK_URL           Telegram bridge webhook
    WEBHOOK_SECRET        Webhook auth secret
    ASO_DB_PATH           SQLite checkpoint path (default: aso_checkpoints.db)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("aso.runner")

THREAD_ID = "aso-main"


def _db_path() -> str:
    return os.environ.get("ASO_DB_PATH", "aso_checkpoints.db")


def _config() -> dict:
    return {"configurable": {"thread_id": THREAD_ID}}


async def run_cycle(*, dry_run: bool = False) -> None:
    """Execute one full ASO pipeline cycle."""
    from .graph import compile_graph
    from .state import ASOState

    db = _db_path()
    log.info("Starting ASO cycle (db: %s, dry_run: %s)", db, dry_run)

    graph = compile_graph(sqlite_path=db)

    initial_state: ASOState = {
        "app_id_ios": os.environ.get("ASC_APP_ID", ""),
        "package_android": os.environ.get("PLAY_PACKAGE_NAME", "com.example.app"),
        "phase": "start",
        "error": None,
        "opportunities": [],
        "selected_opportunity": None,
        "optimization_plan": None,
        "human_feedback": None,
        "changes_applied": None,
        "baseline_metrics": None,
        "post_metrics": None,
        "evaluation": None,
        "messages": [],
    }

    if dry_run:
        import aso.nodes.notify as notify_mod
        import aso.nodes.review as review_mod
        original_notify = notify_mod._send_telegram
        original_review = review_mod._send_telegram
        notify_mod._send_telegram = lambda text: log.info("DRY RUN notify:\n%s", text)
        review_mod._send_telegram = lambda text: log.info("DRY RUN review:\n%s", text)

    try:
        result = await graph.ainvoke(initial_state, _config())

        phase = result.get("phase", "?")
        cycle_id = result.get("cycle_id", "?")
        opportunities = result.get("opportunities", [])
        selected = result.get("selected_opportunity")

        print(f"\n{'='*50}")
        print(f"ASO Cycle #{cycle_id}")
        print(f"Phase: {phase}")
        print(f"Opportunities: {len(opportunities)}")

        if selected:
            print(f"Selected: [{selected.get('type')}] {selected.get('description', '')[:100]}")

            if phase == "notified":
                print("Status: complete")
            else:
                print("Status: waiting for input (/aso approve|reject|skip)")
        else:
            print("No action needed.")

        print(f"{'='*50}\n")

    except Exception:
        log.exception("ASO cycle failed")
        raise
    finally:
        if dry_run:
            notify_mod._send_telegram = original_notify
            review_mod._send_telegram = original_review


async def resume_graph(human_input: dict) -> None:
    """Resume a paused graph with human input.

    Used for both review (approve/reject/skip) and wait (resume).
    """
    from langgraph.types import Command

    from .graph import compile_graph

    db = _db_path()
    graph = compile_graph(sqlite_path=db)

    log.info("Resuming graph with input: %s", human_input)

    try:
        result = await graph.ainvoke(Command(resume=human_input), _config())

        phase = result.get("phase", "?")
        print(f"\nGraph resumed. Phase: {phase}")

        if phase == "notified":
            eval_data = result.get("evaluation")
            if eval_data:
                print(f"Verdict: {eval_data.get('verdict', '?')}")
        elif phase in ("revision", "approved"):
            print("Pipeline continuing...")

    except Exception:
        log.exception("Resume failed")
        raise


async def show_status() -> None:
    """Show current pipeline state from checkpoint."""
    from .graph import compile_graph

    db = _db_path()
    if not Path(db).exists():
        print("No checkpoint database found. Run a cycle first.")
        return

    graph = compile_graph(sqlite_path=db)
    state = await graph.aget_state(_config())

    if not state or not state.values:
        print("No saved state found.")
        return

    values = state.values
    next_nodes = state.next  # what nodes are pending

    print(f"\n{'='*40}")
    print("ASO Pipeline Status")
    print(f"{'='*40}")
    print(f"Cycle:       #{values.get('cycle_id', '—')}")
    print(f"Phase:       {values.get('phase', '—')}")
    print(f"Next nodes:  {list(next_nodes) if next_nodes else 'complete'}")
    print(f"Error:       {values.get('error') or 'none'}")

    # Opportunities
    opps = values.get("opportunities", [])
    if opps:
        print(f"\nOpportunities ({len(opps)}):")
        for opp in opps:
            print(f"  [{opp.get('priority')}] {opp.get('type')}: {opp.get('description', '')[:80]}")

    # Selected
    selected = values.get("selected_opportunity")
    if selected:
        print(f"\nSelected: [{selected.get('type')}] {selected.get('description', '')[:80]}")

    # Plan
    plan = values.get("optimization_plan")
    if plan:
        changes = plan.get("changes", [])
        print(f"\nPlan: {len(changes)} changes, risk: {plan.get('risk_assessment', '?')}")

    # Changes
    changes = values.get("changes_applied")
    if changes:
        print(f"\nChanges: {changes.get('success_count', 0)} applied, {changes.get('error_count', 0)} errors")

    # Evaluation
    evaluation = values.get("evaluation")
    if evaluation:
        print(f"\nEvaluation: {evaluation.get('verdict', '?')}")

    # Pending actions
    if next_nodes:
        next_list = list(next_nodes)
        if "review" in next_list or "__interrupt__" in str(state.tasks):
            print("\n⏸️  Ожидает действия: /aso approve | reject | skip")
        elif "wait" in next_list:
            print("\n⏸️  Ожидает окончания периода измерения: /aso resume")

    print(f"{'='*40}\n")


async def check_wait_resume() -> None:
    """Check if a waiting graph should be auto-resumed.

    Called by cron to check if the measurement period has elapsed.
    """
    from .graph import compile_graph

    db = _db_path()
    if not Path(db).exists():
        return

    graph = compile_graph(sqlite_path=db)
    state = await graph.aget_state(_config())

    if not state or not state.values:
        return

    # Check if graph is paused at wait node
    tasks = state.tasks or ()
    for task in tasks:
        interrupts = getattr(task, "interrupts", [])
        for intr in interrupts:
            intr_value = getattr(intr, "value", {})
            if isinstance(intr_value, dict) and intr_value.get("type") == "scheduled_wait":
                resume_at = intr_value.get("resume_at", "")
                if resume_at:
                    resume_dt = datetime.fromisoformat(resume_at)
                    now = datetime.now(UTC)
                    if now >= resume_dt:
                        log.info("Wait period elapsed, auto-resuming graph")
                        await resume_graph({"action": "resume"})
                    else:
                        remaining = (resume_dt - now).days
                        log.info("Wait period not elapsed, %d days remaining", remaining)
                return

    log.debug("No waiting graph found")


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0]

    if cmd == "run":
        dry_run = "--dry-run" in args
        asyncio.run(run_cycle(dry_run=dry_run))

    elif cmd == "approve":
        asyncio.run(resume_graph({"action": "approve"}))

    elif cmd == "reject":
        comment = " ".join(args[1:]) if len(args) > 1 else ""
        asyncio.run(resume_graph({"action": "reject", "comment": comment}))

    elif cmd == "skip":
        asyncio.run(resume_graph({"action": "skip"}))

    elif cmd == "resume":
        asyncio.run(resume_graph({"action": "resume"}))

    elif cmd == "check-wait":
        asyncio.run(check_wait_resume())

    elif cmd == "status":
        asyncio.run(show_status())

    elif cmd == "history":
        print("History: coming soon (use 'status' for current state)")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
