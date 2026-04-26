"""Dashboard configuration."""

import os

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8789"))
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "kronos2026")
