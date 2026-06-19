"""Eval test path bootstrap.

Pytest imports tests in nested directories with that directory at the front of
``sys.path``. Add the app root so evals can import ``kronos`` when run alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
