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
from starlette.exceptions import HTTPException as StarletteHTTPException

from dashboard.api.agents import router as agents_router
from dashboard.api.anomalies import router as anomalies_router
from dashboard.api.audit_trail import router as audit_trail_router
from dashboard.api.config import router as config_router
from dashboard.api.graph import router as graph_router
from dashboard.api.graph import set_agent
from dashboard.api.health import router as health_router
from dashboard.api.mcp import router as mcp_router
from dashboard.api.memory import router as memory_router
from dashboard.api.monitoring import router as monitoring_router
from dashboard.api.monitoring import set_scheduler
from dashboard.api.overview import router as overview_router
from dashboard.api.performance import router as performance_router
from dashboard.api.persona import router as persona_router
from dashboard.api.skills import router as skills_router
from dashboard.api.swarm import router as swarm_router
from dashboard.config import (
    DASHBOARD_HOST,
    DASHBOARD_PASSWORD,
    DASHBOARD_PASSWORD_GENERATED,
    DASHBOARD_PORT,
    DASHBOARD_USERNAME,
)
from dashboard.ws.handlers import install_log_handler, ws_logs

log = logging.getLogger("kronos.dashboard")


class SPAStaticFiles(StaticFiles):
    """Serve index.html for client-side dashboard routes."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and scope.get("method") in {"GET", "HEAD"}:
                return await super().get_response("index.html", scope)
            raise


def create_app(scheduler=None, agent=None) -> FastAPI:
    """Create FastAPI dashboard app."""
    app = FastAPI(
        title="Kronos Agent OS Dashboard",
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
    app.include_router(swarm_router)

    # WebSocket
    app.websocket("/ws/logs")(ws_logs)

    if scheduler:
        set_scheduler(scheduler)
    if agent:
        set_agent(agent)

    # Serve React frontend (if built)
    static_dir = Path(__file__).parent.parent / "dashboard-ui" / "dist"
    if static_dir.is_dir():
        app.mount("/", SPAStaticFiles(directory=str(static_dir), html=True), name="ui")
        log.info("Serving UI from %s", static_dir)

    # Install WebSocket log handler
    install_log_handler()

    return app


async def run_dashboard(scheduler=None, agent=None) -> None:
    """Start dashboard server."""
    app = create_app(scheduler=scheduler, agent=agent)
    config = uvicorn.Config(
        app,
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        log_level="warning",  # don't duplicate agent logs
    )
    server = uvicorn.Server(config)
    log.info("Dashboard starting on http://%s:%d", DASHBOARD_HOST, DASHBOARD_PORT)
    if DASHBOARD_PASSWORD_GENERATED:
        log.warning(
            "Generated temporary dashboard password for user '%s': %s",
            DASHBOARD_USERNAME,
            DASHBOARD_PASSWORD,
        )
    await server.serve()
