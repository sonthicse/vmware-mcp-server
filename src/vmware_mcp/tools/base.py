"""Shared plumbing for tool modules."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastmcp.exceptions import ToolError

from ..config import get_config
from ..devices import list_cdroms, list_disks, list_nics
from ..errors import DestructiveOpDisabledError, VmwareMcpError
from ..vmrun import VmRun
from ..vmx import VmxFile

F = TypeVar("F", bound=Callable[..., Any])

# Annotation presets, so every tool declares its blast radius consistently.
READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
MUTATING = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


def tool_errors(func: F) -> F:
    """Turn our domain errors into ToolError so the message reaches the model."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except VmwareMcpError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def require_destructive_enabled(operation: str) -> None:
    if not get_config().allow_destructive:
        raise DestructiveOpDisabledError(
            f"{operation} is blocked. This deletes data that cannot be recovered, so it "
            "is off unless the server is started with VMWARE_MCP_ALLOW_DESTRUCTIVE=1 in "
            "its environment. Ask the user to enable it in their MCP server config."
        )


def saved_result(vmx: VmxFile, changes: dict[str, Any], backup: Path | None) -> dict:
    return {
        "vm": vmx.get("displayName") or vmx.path.stem,
        "vmx_path": str(vmx.path),
        "changes": changes,
        "backup": str(backup) if backup else None,
        "note": "Changes apply the next time the VM powers on.",
    }


def vm_summary(vmx: VmxFile, vmrun: VmRun | None = None, with_disks: bool = True) -> dict:
    vmrun = vmrun or VmRun()
    memory_mb = vmx.get_int("memsize", 0) or 0
    duplicates = vmx.duplicate_keys()
    return {
        # A repeated key makes the .vmx unopenable in VMware, so report it up
        # front rather than letting the reader trust the values below.
        "config_problems": (
            {
                "duplicate_keys": duplicates,
                "impact": "VMware refuses to open a .vmx with a repeated key; this VM "
                "will not power on until the duplicates are removed with unset_vm_config.",
            }
            if duplicates
            else None
        ),
        "name": vmx.get("displayName") or vmx.path.stem,
        "vmx_path": str(vmx.path),
        "power_state": "poweredOn" if vmrun.is_running(vmx.path) else "poweredOff",
        "guest_os": vmx.get("guestOS"),
        "firmware": vmx.get("firmware", "bios"),
        "hardware_version": vmx.get("virtualHW.version"),
        "cpu": {
            "processors": vmx.get_int("numvcpus", 1),
            "cores_per_socket": vmx.get_int("cpuid.coresPerSocket", 1),
            "virtualize_vtx": vmx.get_bool("vhv.enable"),
            "virtualize_iommu": vmx.get_bool("vvtd.enable"),
        },
        "memory_mb": memory_mb,
        "memory_gb": round(memory_mb / 1024, 2),
        "display": {
            "accelerate_3d": vmx.get_bool("mks.enable3d"),
            "graphics_memory_mb": (vmx.get_int("svga.graphicsMemoryKB", 0) or 0) // 1024,
            "vram_mb": (vmx.get_int("svga.vramSize", 0) or 0) // (1024 * 1024),
            "auto_detect_monitors": vmx.get_bool("svga.autodetect", True),
            "num_displays": vmx.get_int("svga.numDisplays"),
        },
        "devices": {
            "usb": vmx.get_bool("usb.present"),
            "sound": vmx.get_bool("sound.present"),
            "floppy": vmx.get_bool("floppy0.present"),
            "vmci": vmx.get_bool("vmci0.present"),
            "shared_folders_enabled": vmx.get_bool("isolation.tools.hgfs.disable") is False,
        },
        "tools": {
            "sync_time": vmx.get_bool("tools.syncTime"),
            "upgrade_policy": vmx.get("tools.upgrade.policy"),
        },
        "disks": list_disks(vmx, include_details=with_disks),
        "cdroms": list_cdroms(vmx),
        "network_adapters": list_nics(vmx),
        "annotation": vmx.get("annotation"),
    }
