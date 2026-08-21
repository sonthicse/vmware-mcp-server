"""Tool modules. Each exposes ``register(mcp)``."""

from __future__ import annotations

from fastmcp import FastMCP

from . import discovery, disks, guest, hardware, lifecycle, network, power

MODULES = (discovery, power, hardware, network, disks, lifecycle, guest)


def register_all(mcp: FastMCP) -> None:
    for module in MODULES:
        module.register(mcp)
