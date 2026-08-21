"""Virtual disk and CD-ROM tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..devices import ensure_controller, list_cdroms, list_disks, next_free_node, vmdk_info
from ..errors import VmwareMcpError
from ..inventory import ensure_allowed, load_vmx, require_offline
from ..vmrun import VmRun, vdiskmanager
from .base import (
    DESTRUCTIVE,
    MUTATING,
    require_destructive_enabled,
    saved_result,
    tool_errors,
)

DISK_TYPE_IDS = {
    "growable": "0",
    "growable-split": "1",
    "preallocated": "2",
    "preallocated-split": "3",
}

# vdiskmanager only understands these three; everything else maps to lsilogic.
_VDISK_ADAPTER = {"ide": "ide", "scsi": "lsilogic", "sata": "lsilogic", "nvme": "lsilogic"}

_SNAPSHOT_LINK_RE = re.compile(r"-\d{6}\.vmdk$", re.IGNORECASE)
_NODE_RE = re.compile(r"^(?:scsi|sata|nvme|ide)\d+:\d+$", re.IGNORECASE)


def _resolve_disk(vmx_dir: Path, file_name: str) -> Path:
    candidate = Path(file_name)
    return candidate if candidate.is_absolute() else vmx_dir / candidate


def _require_node(vmx, node: str) -> dict:
    node = node.strip().lower()
    if not _NODE_RE.match(node):
        raise VmwareMcpError(
            f"'{node}' is not a device node. Expected something like 'scsi0:1' or "
            "'sata0:0'. Call get_vm_info to see the VM's disks."
        )
    for disk in list_disks(vmx, include_details=False):
        if disk["node"] == node:
            return disk
    known = [d["node"] for d in list_disks(vmx, include_details=False)]
    raise VmwareMcpError(
        f"{vmx.path.stem} has no disk at '{node}'. Its disks are: {known or 'none'}."
    )


def _has_snapshots(vmx_path: Path) -> bool:
    try:
        raw = VmRun()("listSnapshots", str(vmx_path))
    except Exception:  # noqa: BLE001 - snapshot state is advisory here
        return False
    first = raw.splitlines()[0] if raw.splitlines() else ""
    digits = re.findall(r"\d+", first)
    return bool(digits) and int(digits[0]) > 0


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Add virtual disk",
        annotations=MUTATING,
        tags={"vmware", "config", "disk"},
    )
    @tool_errors
    def add_disk(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        size_gb: Annotated[
            float | None,
            Field(
                description="Capacity of a NEW disk in GB. Mutually exclusive with "
                "existing_vmdk.",
                gt=0,
                le=8192,
            ),
        ] = None,
        existing_vmdk: Annotated[
            str | None,
            Field(
                description="Path to an EXISTING .vmdk to attach instead of creating one. "
                "Mutually exclusive with size_gb."
            ),
        ] = None,
        controller: Annotated[
            Literal["scsi", "sata", "nvme", "ide"],
            Field(
                description="Controller family to attach to. 'scsi' (LSI Logic) is the "
                "safe default; 'nvme' is fastest but needs guest NVMe support."
            ),
        ] = "scsi",
        disk_type: Annotated[
            Literal["growable", "growable-split", "preallocated", "preallocated-split"],
            Field(
                description="Allocation policy for a new disk. 'growable' expands on "
                "demand in one file; 'preallocated' reserves all space up front and is "
                "faster; the '-split' variants shard into 2 GB files for FAT32/network shares."
            ),
        ] = "growable-split",
        file_name: Annotated[
            str | None,
            Field(
                description="File name for the new disk, relative to the VM folder. "
                "Defaults to '<vm name>-<n>.vmdk'."
            ),
        ] = None,
        independent_mode: Annotated[
            Literal["persistent", "nonpersistent"] | None,
            Field(
                description="Make the disk independent, i.e. excluded from snapshots. "
                "'nonpersistent' discards all writes at power off. Omit for a normal disk."
            ),
        ] = None,
    ) -> dict:
        """Create a new virtual disk (or attach an existing .vmdk) to a powered-off VM.

        Pass exactly one of size_gb (create) or existing_vmdk (attach). The disk
        lands on the next free slot of the chosen controller. To grow a disk that
        already exists use resize_disk instead.
        """
        if (size_gb is None) == (existing_vmdk is None):
            raise VmwareMcpError(
                "Pass exactly one of size_gb (to create a new disk) or existing_vmdk "
                "(to attach one that already exists)."
            )

        vmx = load_vmx(vm)
        require_offline(vmx.path, "Adding a disk")
        vm_dir = vmx.path.parent

        if existing_vmdk is not None:
            disk_path = _resolve_disk(vm_dir, existing_vmdk)
            if not disk_path.is_file():
                raise VmwareMcpError(f"No .vmdk file at {disk_path}.")
            ensure_allowed(disk_path.resolve())
            created = False
        else:
            if file_name:
                if not file_name.lower().endswith(".vmdk"):
                    raise VmwareMcpError("file_name must end in .vmdk")
                disk_path = _resolve_disk(vm_dir, file_name)
            else:
                index = len(list_disks(vmx, include_details=False)) + 1
                disk_path = vm_dir / f"{vmx.path.stem}-{index}.vmdk"
            if disk_path.exists():
                raise VmwareMcpError(
                    f"{disk_path} already exists. Choose another file_name, or pass it as "
                    "existing_vmdk to attach it."
                )
            ensure_allowed(disk_path.parent.resolve())
            vdiskmanager(
                "-c",
                "-s", f"{size_gb}GB",
                "-a", _VDISK_ADAPTER[controller],
                "-t", DISK_TYPE_IDS[disk_type],
                str(disk_path),
            )
            created = True

        try:
            node = next_free_node(vmx, controller)
        except ValueError as exc:
            raise VmwareMcpError(str(exc)) from exc

        ensure_controller(vmx, node)
        vmx.set(f"{node}.present", True)
        try:
            relative = str(disk_path.relative_to(vm_dir))
        except ValueError:
            relative = str(disk_path)
        vmx.set(f"{node}.fileName", relative)
        if independent_mode:
            vmx.set(f"{node}.mode", f"independent-{independent_mode}")

        backup = vmx.save()
        result = saved_result(
            vmx,
            {
                "node": node,
                "file": relative,
                "created": created,
                "size_gb": size_gb,
                "disk_type": disk_type if created else None,
                "independent_mode": independent_mode,
            },
            backup,
        )
        result["disk"] = vmdk_info(disk_path)
        return result

    @mcp.tool(
        title="Detach virtual disk",
        annotations=DESTRUCTIVE,
        tags={"vmware", "config", "disk"},
    )
    @tool_errors
    def detach_disk(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        node: Annotated[
            str, Field(description="Device node from get_vm_info, e.g. 'scsi0:1'.")
        ],
        delete_file: Annotated[
            bool,
            Field(
                description="Also delete the .vmdk from disk. Irreversible, and requires "
                "VMWARE_MCP_ALLOW_DESTRUCTIVE=1. Leave false to keep the file."
            ),
        ] = False,
    ) -> dict:
        """Detach a virtual disk from a powered-off VM, optionally deleting the file.

        Detaching only removes the .vmx entries; the .vmdk stays on disk unless
        delete_file is set. Never detach the boot disk of a VM you still need.
        """
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Detaching a disk")
        disk = _require_node(vmx, node)
        node = disk["node"]

        disk_path = _resolve_disk(vmx.path.parent, disk["file_name"])
        removed_keys = vmx.unset_prefix(f"{node}.")
        backup = vmx.save()

        deleted: list[str] = []
        if delete_file:
            require_destructive_enabled("Deleting a .vmdk file")
            ensure_allowed(disk_path.resolve())
            for part in sorted(disk_path.parent.glob(f"{disk_path.stem}*.vmdk")):
                part.unlink(missing_ok=True)
                deleted.append(str(part))

        result = saved_result(
            vmx,
            {"node": node, "removed_keys": removed_keys, "deleted_files": deleted},
            backup,
        )
        result["file_kept_at"] = None if delete_file else str(disk_path)
        return result

    @mcp.tool(
        title="Resize virtual disk",
        annotations=MUTATING,
        tags={"vmware", "config", "disk"},
    )
    @tool_errors
    def resize_disk(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        node: Annotated[str, Field(description="Device node from get_vm_info, e.g. 'scsi0:0'.")],
        new_size_gb: Annotated[
            float,
            Field(
                description="New total capacity in GB. VMware can only grow a disk, never "
                "shrink it.",
                gt=0,
                le=8192,
            ),
        ],
    ) -> dict:
        """Expand a virtual disk to a larger capacity.

        Only grows -- VMware cannot shrink a .vmdk. The VM must be powered off
        and must have no snapshots, since expanding a snapshot chain is refused.
        Afterwards the guest OS still needs its partition and filesystem grown
        from inside (diskpart / growpart + resize2fs).
        """
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Resizing a disk")
        disk = _require_node(vmx, node)
        disk_path = _resolve_disk(vmx.path.parent, disk["file_name"])

        if _SNAPSHOT_LINK_RE.search(disk_path.name) or _has_snapshots(vmx.path):
            raise VmwareMcpError(
                f"'{vmx.path.stem}' has snapshots, so its disks cannot be expanded "
                f"({disk_path.name} is part of a snapshot chain). Delete every snapshot "
                "first (delete_snapshot), then retry."
            )

        before = vmdk_info(disk_path)
        if before["capacity_gb"] and new_size_gb <= before["capacity_gb"]:
            raise VmwareMcpError(
                f"{disk_path.name} is already {before['capacity_gb']} GB. VMware can only "
                f"grow disks, so new_size_gb must exceed that."
            )

        vdiskmanager("-x", f"{new_size_gb}GB", str(disk_path))
        return {
            "vm": vmx.path.stem,
            "node": node,
            "before": before,
            "after": vmdk_info(disk_path),
            "note": "Grow the partition and filesystem inside the guest to use the new space.",
        }

    @mcp.tool(
        title="Defragment or compact disk",
        annotations=MUTATING,
        tags={"vmware", "config", "disk"},
    )
    @tool_errors
    def optimize_disk(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        node: Annotated[str, Field(description="Device node from get_vm_info, e.g. 'scsi0:0'.")],
        action: Annotated[
            Literal["defragment", "compact"],
            Field(
                description="'compact' reclaims host space freed inside the guest (growable "
                "disks only); 'defragment' reorders blocks for sequential reads."
            ),
        ] = "compact",
    ) -> dict:
        """Compact or defragment a virtual disk to reclaim host disk space.

        The VM must be powered off. Compacting only helps growable disks, and
        only after the guest's free space has been zeroed. Both operations can
        take a long time on large disks -- raise VMWARE_MCP_TIMEOUT if needed.
        """
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Optimizing a disk")
        disk = _require_node(vmx, node)
        disk_path = _resolve_disk(vmx.path.parent, disk["file_name"])

        before = vmdk_info(disk_path)
        vdiskmanager("-k" if action == "compact" else "-d", str(disk_path))
        return {
            "vm": vmx.path.stem,
            "node": node,
            "action": action,
            "before": before,
            "after": vmdk_info(disk_path),
        }

    @mcp.tool(
        title="Attach ISO to CD/DVD drive",
        annotations=MUTATING,
        tags={"vmware", "config", "media"},
    )
    @tool_errors
    def attach_iso(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        iso_path: Annotated[
            str, Field(description="Full path to the .iso file on the host.")
        ],
        node: Annotated[
            str | None,
            Field(
                description="CD/DVD device node such as 'sata0:1'. Omit to reuse the VM's "
                "existing drive, or create one on the next free SATA slot."
            ),
        ] = None,
        connect_at_power_on: Annotated[
            bool, Field(description="Whether the drive is connected when the VM boots.")
        ] = True,
    ) -> dict:
        """Mount an ISO image in the VM's CD/DVD drive.

        Creates a SATA CD-ROM drive if the VM has none. Requires the VM to be
        powered off. Use this to install a guest OS or mount VMware Tools media.
        """
        iso = Path(iso_path)
        if not iso.is_file():
            raise VmwareMcpError(f"No file at {iso}. Give the full path to the .iso.")
        if iso.suffix.lower() != ".iso":
            raise VmwareMcpError(f"{iso.name} is not an .iso image.")

        vmx = load_vmx(vm)
        require_offline(vmx.path, "Attaching an ISO")

        if node is None:
            existing = list_cdroms(vmx)
            node = existing[0]["node"] if existing else next_free_node(vmx, "sata")
        node = node.strip().lower()
        if not _NODE_RE.match(node):
            raise VmwareMcpError(f"'{node}' is not a device node, e.g. 'sata0:1'.")

        ensure_controller(vmx, node)
        vmx.update(
            {
                f"{node}.present": True,
                f"{node}.deviceType": "cdrom-image",
                f"{node}.fileName": str(iso),
                f"{node}.startConnected": connect_at_power_on,
            }
        )
        backup = vmx.save()
        return saved_result(vmx, {"node": node, "iso": str(iso)}, backup)

    @mcp.tool(
        title="Eject CD/DVD media",
        annotations=MUTATING,
        tags={"vmware", "config", "media"},
    )
    @tool_errors
    def detach_iso(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        node: Annotated[
            str | None,
            Field(description="CD/DVD node to eject. Omit to eject every CD/DVD drive."),
        ] = None,
        remove_drive: Annotated[
            bool,
            Field(
                description="Remove the drive entirely instead of just ejecting the ISO. "
                "Do this before first boot from disk to skip the CD boot attempt."
            ),
        ] = False,
    ) -> dict:
        """Eject the ISO from a VM's CD/DVD drive, or remove the drive altogether.

        Requires the VM to be powered off. Ejecting leaves an empty drive
        pointing at the host's physical CD device.
        """
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Ejecting CD/DVD media")

        drives = list_cdroms(vmx)
        if node:
            node = node.strip().lower()
            drives = [d for d in drives if d["node"] == node]
            if not drives:
                raise VmwareMcpError(f"{vmx.path.stem} has no CD/DVD drive at '{node}'.")
        if not drives:
            raise VmwareMcpError(f"{vmx.path.stem} has no CD/DVD drives to eject.")

        affected = []
        for drive in drives:
            target = drive["node"]
            if remove_drive:
                vmx.unset_prefix(f"{target}.")
            else:
                vmx.set(f"{target}.deviceType", "cdrom-raw")
                vmx.set(f"{target}.fileName", "auto detect")
                vmx.set(f"{target}.startConnected", False)
            affected.append(target)

        backup = vmx.save()
        return saved_result(
            vmx, {"nodes": affected, "removed": remove_drive}, backup
        )
