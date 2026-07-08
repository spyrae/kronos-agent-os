"""Kronos Agent OS — self-hosted runtime for durable AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject.toml.
    __version__ = version("kronos-agent-os")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
