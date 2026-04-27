#!/usr/bin/env python3
"""Seed deterministic KAOS dashboard demo state."""

from __future__ import annotations

import sys

from kronos.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo-seed", *sys.argv[1:]]))
