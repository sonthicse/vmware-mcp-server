"""Power state and snapshot tools."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..inventory import resolve_vmx
from ..vmrun import VmRun
from .base import DESTRUCTIVE, MUTATING, READ_ONLY, require_destructive_enabled, tool_errors

# action -> (vmrun command, accepts hard/soft modifier)
_ACTIONS: dict[str, tuple[str, bool]] = {
    "start": ("start", False),
    "stop": ("stop", True),
    "reset": ("reset", True),
    "suspend": ("suspend", True),
    "pause": ("pause", False),
    "unpause": ("unpause", False),
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Change VM power state",
        annotations=MUTATING,
        tags={"vmware", "power"},
    )
    @tool_errors
    def power_vm(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        action: Annotated[
            Literal["start", "stop", "reset", "suspend", "pause", "unpause"],
            Field(
                description="start powers on; stop shuts down; reset reboots; suspend "
                "saves state to disk; pause/unpause freeze and resume the vCPUs."
            ),
        ],
        mode: Annotated[
            Literal["soft", "hard"],
            Field(
                description="Applies to stop/reset/suspend only. 'soft' asks the guest OS "
                "via VMware Tools for a clean shutdown; 'hard' is the equivalent of "
                "pulling the power cord and can corrupt the guest filesystem."
            ),
        ] = "soft",
        gui: Annotated[
            bool,
            Field(
                description="Applies to start only. True opens the VM window in the "
                "Workstation UI; False boots it headless in the background."
            ),
        ] = True,
    ) -> dict:
        """Power a VM on, off, reset, suspend, pause, or unpause it.

        Hardware edits (set_vm_hardware, add_disk, ...) require the VM to be
        powered off, so call this with action='stop' first. Prefer mode='soft'
        unless the guest is unresponsive.
        """
        vmx_path = resolve_vmx(vm)
        command, takes_mode = _ACTIONS[action]

        args = [str(vmx_path)]
        if action == "start":
            args.append("gui" if gui else "nogui")
        elif takes_mode:
            args.append(mode)

        vmrun = VmRun()
        vmrun(command, *args)
        return {
            "vm": vmx_path.stem,
            "vmx_path": str(vmx_path),
            "action": action,
            "mode": mode if takes_mode else None,
            "power_state": "poweredOn" if vmrun.is_running(vmx_path) else "poweredOff",
        }

    @mcp.tool(
        title="List snapshots",
        annotations=READ_ONLY,
        tags={"vmware", "read", "snapshot"},
    )
    @tool_errors
    def list_snapshots(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        show_tree: Annotated[
            bool, Field(description="Render parent/child nesting instead of a flat list.")
        ] = True,
    ) -> dict:
        """List a VM's snapshots by name.

        Snapshot names returned here are what revert_snapshot and delete_snapshot
        expect. Nested snapshots are addressed with a path like 'base/patched'.
        """
        vmx_path = resolve_vmx(vm)
        args = [str(vmx_path)]
        if show_tree:
            args.append("showTree")
        raw = VmRun()("listSnapshots", *args)
        lines = raw.splitlines()
        names = [line.strip() for line in lines[1:] if line.strip()]
        return {
            "vm": vmx_path.stem,
            "count": len(names),
            "snapshots": names,
            "raw": raw,
        }

    @mcp.tool(
        title="Create snapshot",
        annotations=MUTATING,
        tags={"vmware", "snapshot"},
    )
    @tool_errors
    def create_snapshot(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        name: Annotated[
            str,
            Field(
                description="Snapshot name. Avoid '/' -- it separates levels when "
                "addressing nested snapshots.",
                min_length=1,
                max_length=80,
            ),
        ],
    ) -> dict:
        """Take a snapshot of the VM's current state.

        Works powered on or off. Take one before any risky change -- most
        operations here are otherwise irreversible.
        """
        vmx_path = resolve_vmx(vm)
        VmRun()("snapshot", str(vmx_path), name)
        return {"vm": vmx_path.stem, "snapshot": name, "created": True}

    @mcp.tool(
        title="Revert to snapshot",
        annotations=DESTRUCTIVE,
        tags={"vmware", "snapshot"},
    )
    @tool_errors
    def revert_snapshot(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        name: Annotated[
            str,
            Field(
                description="Snapshot name from list_snapshots. Use 'parent/child' for "
                "a nested snapshot."
            ),
        ],
    ) -> dict:
        """Roll the VM back to a snapshot, discarding all changes made since.

        Everything written to the VM's disks after that snapshot is lost. Take a
        fresh snapshot first if the current state matters.
        """
        vmx_path = resolve_vmx(vm)
        VmRun()("revertToSnapshot", str(vmx_path), name)
        return {
            "vm": vmx_path.stem,
            "snapshot": name,
            "reverted": True,
            "note": "The VM is now powered off at the snapshot state; start it to resume.",
        }

    @mcp.tool(
        title="Delete snapshot",
        annotations=DESTRUCTIVE,
        tags={"vmware", "snapshot"},
    )
    @tool_errors
    def delete_snapshot(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        name: Annotated[str, Field(description="Snapshot name from list_snapshots.")],
        delete_children: Annotated[
            bool,
            Field(description="Also delete every snapshot nested beneath this one."),
        ] = False,
    ) -> dict:
        """Delete a snapshot, consolidating its data into the parent.

        Requires VMWARE_MCP_ALLOW_DESTRUCTIVE=1. This does not revert the VM --
        it only removes the ability to go back to that point.
        """
        require_destructive_enabled("delete_snapshot")
        vmx_path = resolve_vmx(vm)
        args = [str(vmx_path), name]
        if delete_children:
            args.append("andDeleteChildren")
        VmRun()("deleteSnapshot", *args)
        return {"vm": vmx_path.stem, "snapshot": name, "deleted": True}
