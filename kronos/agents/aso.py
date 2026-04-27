"""ASO pipeline adapter — thin wrapper for Kronos Agent OS integration.

Provides async functions to run ASO commands from bridge or supervisor.
The ASO pipeline runs as an independent sub-graph with its own checkpointer.
"""

import logging
import os

log = logging.getLogger("kronos.agents.aso")

# ASO uses its own SQLite checkpointer (separate from main agent)
ASO_DB_PATH = os.environ.get("ASO_DB_PATH", "data/aso_checkpoints.db")


async def aso_run(dry_run: bool = False) -> str:
    """Run one full ASO cycle. Returns status text."""
    from aso.runner import run_cycle

    os.environ.setdefault("ASO_DB_PATH", ASO_DB_PATH)

    try:
        await run_cycle(dry_run=dry_run)
        return "ASO cycle started."
    except Exception as e:
        log.exception("ASO run failed")
        return f"ASO run failed: {e}"


async def aso_approve() -> str:
    """Approve pending ASO plan."""
    from aso.runner import resume_graph

    os.environ.setdefault("ASO_DB_PATH", ASO_DB_PATH)

    try:
        await resume_graph({"action": "approve"})
        return "ASO plan approved, executing..."
    except Exception as e:
        log.exception("ASO approve failed")
        return f"ASO approve failed: {e}"


async def aso_reject(comment: str = "") -> str:
    """Reject pending ASO plan with optional feedback."""
    from aso.runner import resume_graph

    os.environ.setdefault("ASO_DB_PATH", ASO_DB_PATH)

    try:
        await resume_graph({"action": "reject", "comment": comment})
        return "ASO plan rejected, replanning..."
    except Exception as e:
        log.exception("ASO reject failed")
        return f"ASO reject failed: {e}"


async def aso_skip() -> str:
    """Skip current ASO cycle."""
    from aso.runner import resume_graph

    os.environ.setdefault("ASO_DB_PATH", ASO_DB_PATH)

    try:
        await resume_graph({"action": "skip"})
        return "ASO cycle skipped."
    except Exception as e:
        log.exception("ASO skip failed")
        return f"ASO skip failed: {e}"


async def aso_resume() -> str:
    """Resume ASO after wait period."""
    from aso.runner import resume_graph

    os.environ.setdefault("ASO_DB_PATH", ASO_DB_PATH)

    try:
        await resume_graph({"action": "resume"})
        return "ASO resumed."
    except Exception as e:
        log.exception("ASO resume failed")
        return f"ASO resume failed: {e}"


async def aso_status() -> str:
    """Get current ASO pipeline status. Returns formatted text."""
    from pathlib import Path

    os.environ.setdefault("ASO_DB_PATH", ASO_DB_PATH)

    db = os.environ.get("ASO_DB_PATH", ASO_DB_PATH)
    if not Path(db).exists():
        return "ASO: нет checkpoint базы. Запусти /aso run"

    from aso.graph import compile_graph

    try:
        graph = compile_graph(sqlite_path=db)
        config = {"configurable": {"thread_id": "aso-main"}}
        state = await graph.aget_state(config)

        if not state or not state.values:
            return "ASO: нет сохранённого состояния."

        v = state.values
        next_nodes = list(state.next) if state.next else []

        lines = [
            "ASO Pipeline Status",
            f"Cycle: #{v.get('cycle_id', '—')}",
            f"Phase: {v.get('phase', '—')}",
            f"Next: {next_nodes or 'complete'}",
        ]

        if v.get("error"):
            lines.append(f"Error: {v['error']}")

        opps = v.get("opportunities", [])
        if opps:
            lines.append(f"Opportunities: {len(opps)}")

        selected = v.get("selected_opportunity")
        if selected:
            lines.append(f"Selected: [{selected.get('type')}] {selected.get('description', '')[:80]}")

        if next_nodes:
            if "review" in next_nodes or "__interrupt__" in str(state.tasks):
                lines.append("\nОжидает: /aso approve | reject | skip")
            elif "wait" in next_nodes:
                lines.append("\nОжидает окончания измерений: /aso resume")

        return "\n".join(lines)

    except Exception as e:
        log.exception("ASO status failed")
        return f"ASO status error: {e}"
