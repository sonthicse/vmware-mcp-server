"""MCP server for VMware Workstation Pro."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "build_server", "main"]


def __getattr__(name: str):  # lazy so `import vmware_mcp` stays cheap
    if name in {"build_server", "main"}:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
