"""CLI test mode — interact with agent without Telegram.

Usage:
    python -m kronos.cli              # without MCP tools
    python -m kronos.cli --tools      # with MCP tools
"""

import asyncio
import logging
import sys

from kronos.config import settings
from kronos.graph import KronosAgent
from kronos.session import SessionStore
from kronos.tools.manager import managed_mcp_tools

log = logging.getLogger("kronos.cli")


async def run_cli():
    """Interactive CLI for testing the agent."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    use_tools = "--tools" in sys.argv
    log.info("Kronos II CLI mode (workspace: %s, tools: %s)", settings.workspace_path, use_tools)

    if use_tools:
        ctx = managed_mcp_tools()
    else:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _no_tools():
            yield []

        ctx = _no_tools()

    async with ctx as tools:
        session_store = SessionStore(settings.db_path, agent_name=settings.agent_name)
        agent = KronosAgent(
            tools=tools or None,
            session_store=session_store,
        )
        log.info("Agent ready (%d tools). Type messages, Ctrl+C to exit.\n", len(tools))

        thread_id = "cli-test"

        while True:
            try:
                user_input = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "/q"):
                break

            if user_input.lower() in ("/clear", "/reset"):
                result = await agent.clear_context(thread_id)
                print(f"\n{result}")
                continue

            try:
                reply = await agent.ainvoke(
                    message=user_input,
                    thread_id=thread_id,
                    user_id="cli-user",
                    session_id="cli-session",
                )
                print(f"\nKronos: {reply}")
            except Exception as e:
                print(f"\n[Error] {e}")


def main():
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
