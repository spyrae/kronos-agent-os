"""Dashboard FastAPI server.

Runs in the same process as the agent. Provides REST API + WebSocket
for managing MCP servers, skills, agents, persona, and monitoring.
"""

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from dashboard.api.agents import router as agents_router
from dashboard.api.anomalies import router as anomalies_router
from dashboard.api.audit_trail import router as audit_trail_router
from dashboard.api.config import router as config_router
from dashboard.api.memory import router as memory_router
from dashboard.api.graph import router as graph_router, set_agent
from dashboard.api.health import router as health_router
from dashboard.api.mcp import router as mcp_router
from dashboard.api.monitoring import router as monitoring_router, set_scheduler
from dashboard.api.overview import router as overview_router
from dashboard.api.performance import router as performance_router
from dashboard.api.persona import router as persona_router
from dashboard.api.skills import router as skills_router
from dashboard.config import DASHBOARD_PORT
from dashboard.ws.handlers import install_log_handler, ws_logs

log = logging.getLogger("kronos.dashboard")


def create_app(scheduler=None, agent=None) -> FastAPI:
    """Create FastAPI dashboard app."""
    app = FastAPI(
        title="Kronos II Dashboard",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    # CORS for local dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routers
    app.include_router(health_router)
    app.include_router(overview_router)
    app.include_router(performance_router)
    app.include_router(audit_trail_router)
    app.include_router(anomalies_router)
    app.include_router(persona_router)
    app.include_router(monitoring_router)
    app.include_router(mcp_router)
    app.include_router(skills_router)
    app.include_router(agents_router)
    app.include_router(graph_router)
    app.include_router(config_router)
    app.include_router(memory_router)

    # WebSocket
    app.websocket("/ws/logs")(ws_logs)

    if scheduler:
        set_scheduler(scheduler)
    if agent:
        set_agent(agent)

    # Serve React frontend (if built)
    static_dir = Path(__file__).parent.parent / "dashboard-ui" / "dist"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")
        log.info("Serving UI from %s", static_dir)

    # Install WebSocket log handler
    install_log_handler()

    return app


async def run_dashboard(scheduler=None, agent=None) -> None:
    """Start dashboard server."""
    app = create_app(scheduler=scheduler, agent=agent)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=DASHBOARD_PORT,
        log_level="warning",  # don't duplicate agent logs
    )
    server = uvicorn.Server(config)
    log.info("Dashboard starting on port %d", DASHBOARD_PORT)
    await server.serve()
