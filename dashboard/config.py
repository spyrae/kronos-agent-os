"""Dashboard configuration."""

import os
import secrets

DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8789"))
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD") or secrets.token_urlsafe(24)
DASHBOARD_PASSWORD_GENERATED = "DASHBOARD_PASSWORD" not in os.environ
