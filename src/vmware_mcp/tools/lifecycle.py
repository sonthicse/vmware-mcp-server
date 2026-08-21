"""Creating, cloning, and deleting whole VMs."""

from __future__ import annotations

import contextlib
import re
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..config import get_config
from ..devices import vmdk_info
from ..errors import VmwareMcpError
from ..inventory import ensure_allowed, resolve_vmx
from ..vmrun import VmRun, vdiskmanager
from ..vmx import backup_files, new_vmx
from .base import DESTRUCTIVE, MUTATING, require_destructive_enabled, tool_errors
from .disks import DISK_TYPE_IDS

_INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

DISK_CONTROLLERS = {
    "nvme": ("nvme0", "nvme"),
    "scsi": ("scsi0", "lsilogic"),
    "sata": ("sata0", None),
    "ide": ("ide0", None),
}


def _build_vmx_entries(
    *,
    name: str,
    guest_os: str,
    cpus: int,
    cores_per_socket: int,
    memory_mb: int,
    firmware: str,
    secure_boot: bool,
    controller: str,
    disk_file: str,
    network_type: str,
    vmnet: str | None,
    iso_path: str | None,
    hardware_version: int,
) -> dict[str, str | int | bool]:
    controller_name, virtual_dev = DISK_CONTROLLERS[controller]
    disk_node = f"{controller_name}:0"

    entries: dict[str, str | int | bool] = {
        ".encoding": "UTF-8",
        "config.version": "8",
        "virtualHW.version": hardware_version,
        "virtualHW.productCompatibility": "hosted",
        "displayName": name,
        "guestOS": guest_os,
        "nvram": f"{name}.nvram",
        "extendedConfigFile": f"{name}.vmxf",
        # CPU / memory
        "numvcpus": cpus,
        "cpuid.coresPerSocket": cores_per_socket,
        "vcpu.hotadd": True,
        "memsize": memory_mb,
        "mem.hotadd": True,
        # Chipset
        "pciBridge0.present": True,
        "pciBridge4.present": True,
        "pciBridge4.virtualDev": "pcieRootPort",
        "pciBridge4.functions": "8",
        "pciBridge5.present": True,
        "pciBridge5.virtualDev": "pcieRootPort",
        "pciBridge5.functions": "8",
        "pciBridge6.present": True,
        "pciBridge6.virtualDev": "pcieRootPort",
        "pciBridge6.functions": "8",
        "pciBridge7.present": True,
        "pciBridge7.virtualDev": "pcieRootPort",
        "pciBridge7.functions": "8",
        "vmci0.present": True,
        "hpet0.present": True,
        # Power behaviour
        "powerType.powerOff": "soft",
        "powerType.powerOn": "soft",
        "powerType.suspend": "soft",
        "powerType.reset": "soft",
        # Storage
        f"{controller_name}.present": True,
        f"{disk_node}.present": True,
        f"{disk_node}.fileName": disk_file,
        # Peripherals
        "usb.present": True,
        "ehci.present": True,
        "usb.vbluetooth.startConnected": True,
        "sound.present": True,
        "sound.autoDetect": True,
        "sound.fileName": "-1",
        "floppy0.present": False,
        "mks.enable3d": True,
        "svga.graphicsMemoryKB": 8388608,
        # Guest integration
        "tools.syncTime": False,
        "tools.upgrade.policy": "upgradeAtPowerCycle",
        # Networking
        "ethernet0.present": True,
        "ethernet0.connectionType": network_type,
        "ethernet0.virtualDev": "e1000e",
        "ethernet0.addressType": "generated",
        "ethernet0.startConnected": True,
    }

    if virtual_dev:
        entries[f"{controller_name}.virtualDev"] = virtual_dev
    if firmware == "efi":
        entries["firmware"] = "efi"
        entries["uefi.secureBoot.enabled"] = secure_boot
    if network_type == "custom" and vmnet:
        entries["ethernet0.vnet"] = vmnet
        entries["ethernet0.displayName"] = vmnet

    # A CD-ROM always exists so the VM can boot an installer later.
    entries["sata0.present"] = True
    entries["sata0:0.present"] = True
    if iso_path:
        entries["sata0:0.deviceType"] = "cdrom-image"
        entries["sata0:0.fileName"] = iso_path
        entries["sata0:0.startConnected"] = True
    else:
        entries["sata0:0.deviceType"] = "cdrom-raw"
        entries["sata0:0.fileName"] = "auto detect"
        entries["sata0:0.startConnected"] = False

    return entries


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Create virtual machine",
        annotations=MUTATING,
        tags={"vmware", "lifecycle"},
    )
    @tool_errors
    def create_vm(
        name: Annotated[
            str,
            Field(
                description="VM name. Used for the folder, the .vmx file, and the library "
                "label, so it cannot contain \\ / : * ? \" < > |.",
                min_length=1,
                max_length=80,
            ),
        ],
        guest_os: Annotated[
            str,
            Field(
                description="VMware guest OS identifier. Common values: 'windows11-64', "
                "'windows10-64', 'windows2022srv-64', 'ubuntu-64', 'debian12-64', "
                "'rhel9-64', 'centos9-64', 'otherlinux-64', 'other-64', 'freebsd-64'."
            ),
        ] = "otherlinux-64",
        cpus: Annotated[int, Field(description="Virtual processors.", ge=1, le=128)] = 2,
        cores_per_socket: Annotated[
            int, Field(description="Cores per socket; must divide `cpus`.", ge=1, le=64)
        ] = 1,
        memory_mb: Annotated[
            int, Field(description="RAM in MB; must be a multiple of 4.", ge=32, le=1024 * 1024)
        ] = 4096,
        disk_size_gb: Annotated[
            float, Field(description="Boot disk capacity in GB.", gt=0, le=8192)
        ] = 60,
        disk_type: Annotated[
            Literal["growable", "growable-split", "preallocated", "preallocated-split"],
            Field(description="Boot disk allocation policy. See add_disk."),
        ] = "growable-split",
        disk_controller: Annotated[
            Literal["nvme", "scsi", "sata", "ide"],
            Field(
                description="Boot disk controller. 'nvme' has inbox drivers in Windows 8+ "
                "and Linux 3.3+ and is fastest; use 'scsi' only for older guests, which "
                "then need a driver at install time."
            ),
        ] = "nvme",
        firmware: Annotated[
            Literal["efi", "bios"],
            Field(
                description="Boot firmware. 'efi' is required by Windows 11 and is the "
                "modern default; 'bios' suits legacy guests."
            ),
        ] = "efi",
        secure_boot: Annotated[
            bool, Field(description="Enable UEFI Secure Boot. Ignored when firmware='bios'.")
        ] = False,
        network_type: Annotated[
            Literal["nat", "bridged", "hostonly", "custom", "none"],
            Field(description="How the first network adapter attaches to the host."),
        ] = "nat",
        vmnet: Annotated[
            str | None, Field(description="Host network for network_type='custom', e.g. 'VMnet2'.")
        ] = None,
        iso_path: Annotated[
            str | None,
            Field(
                description="Installer ISO to mount in the CD/DVD drive. Omit to create the "
                "VM with an empty drive."
            ),
        ] = None,
        directory: Annotated[
            str | None,
            Field(
                description="Parent folder for the VM. A subfolder named after the VM is "
                "created inside it. Defaults to VMware's usual Virtual Machines folder."
            ),
        ] = None,
        hardware_version: Annotated[
            int,
            Field(
                description="Virtual hardware version. 21 matches Workstation 17.5+; "
                "lower it for compatibility with older VMware products.",
                ge=10,
                le=21,
            ),
        ] = 21,
    ) -> dict:
        """Create a new virtual machine: folder, .vmx, and an empty boot disk.

        The VM is created powered off with nothing installed. Mount an installer
        with iso_path (or attach_iso later), then power_vm with action='start'.
        Starting it once is also what makes it appear in the Workstation library.

        Note: no virtual TPM is added, so a stock Windows 11 installer needs its
        TPM check bypassed. Add a vTPM from the Workstation UI (it requires VM
        encryption) if you need one.
        """
        if _INVALID_NAME_RE.search(name):
            raise VmwareMcpError(
                f"'{name}' contains characters Windows forbids in file names "
                '(\\ / : * ? " < > |). Pick a simpler name.'
            )
        if cpus % cores_per_socket != 0:
            raise VmwareMcpError(
                f"cores_per_socket={cores_per_socket} does not divide cpus={cpus} evenly."
            )
        if memory_mb % 4 != 0:
            raise VmwareMcpError(f"memory_mb must be a multiple of 4; got {memory_mb}.")
        if network_type == "custom" and not vmnet:
            raise VmwareMcpError("network_type='custom' needs a `vmnet` such as 'VMnet2'.")

        iso: str | None = None
        if iso_path:
            candidate = Path(iso_path)
            if not candidate.is_file():
                raise VmwareMcpError(f"No ISO at {candidate}.")
            iso = str(candidate)

        config = get_config()
        parent = Path(directory) if directory else config.default_vm_dir
        ensure_allowed(parent.resolve() if parent.exists() else parent)

        vm_dir = parent / name
        if vm_dir.exists() and any(vm_dir.iterdir()):
            raise VmwareMcpError(
                f"{vm_dir} already exists and is not empty. Choose another name or "
                "directory."
            )
        vm_dir.mkdir(parents=True, exist_ok=True)

        disk_file = f"{name}.vmdk"
        disk_path = vm_dir / disk_file

        try:
            vdiskmanager(
                "-c",
                "-s", f"{disk_size_gb}GB",
                "-a", "ide" if disk_controller == "ide" else "lsilogic",
                "-t", DISK_TYPE_IDS[disk_type],
                str(disk_path),
            )

            vmx_path = vm_dir / f"{name}.vmx"
            vmx = new_vmx(
                vmx_path,
                _build_vmx_entries(
                    name=name,
                    guest_os=guest_os,
                    cpus=cpus,
                    cores_per_socket=cores_per_socket,
                    memory_mb=memory_mb,
                    firmware=firmware,
                    secure_boot=secure_boot,
                    controller=disk_controller,
                    disk_file=disk_file,
                    network_type=network_type,
                    vmnet=vmnet,
                    iso_path=iso,
                    hardware_version=hardware_version,
                ),
            )
            vmx.save(backup=False)
        except Exception:
            # Do not leave a half-built VM folder behind.
            shutil.rmtree(vm_dir, ignore_errors=True)
            raise

        return {
            "name": name,
            "vmx_path": str(vmx_path),
            "directory": str(vm_dir),
            "guest_os": guest_os,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "firmware": firmware,
            "disk": vmdk_info(disk_path),
            "iso": iso,
            "power_state": "poweredOff",
            "next_step": "Call power_vm with action='start' to boot the installer.",
        }

    @mcp.tool(
        title="Clone virtual machine",
        annotations=MUTATING,
        tags={"vmware", "lifecycle"},
    )
    @tool_errors
    def clone_vm(
        vm: Annotated[str, Field(description="Source VM: display name, folder, or .vmx path.")],
        new_name: Annotated[
            str,
            Field(
                description="Name for the clone. Used for its folder, .vmx, and library "
                "label.",
                min_length=1,
                max_length=80,
            ),
        ],
        clone_type: Annotated[
            Literal["full", "linked"],
            Field(
                description="'full' copies every disk and is independent but slow and "
                "large; 'linked' shares the parent's disks, is near-instant, but breaks "
                "if the parent is deleted or modified."
            ),
        ] = "full",
        snapshot: Annotated[
            str | None,
            Field(
                description="Clone from this snapshot instead of the current state. "
                "Required by some VMware builds for linked clones."
            ),
        ] = None,
        directory: Annotated[
            str | None,
            Field(description="Parent folder for the clone. Defaults to the source VM's parent."),
        ] = None,
    ) -> dict:
        """Clone a VM, either as a full independent copy or a linked clone.

        The source VM must be powered off. A linked clone stays dependent on the
        source's disks forever -- deleting the source breaks it.
        """
        if _INVALID_NAME_RE.search(new_name):
            raise VmwareMcpError(f"'{new_name}' contains characters Windows forbids in names.")

        source = resolve_vmx(vm)
        vmrun = VmRun()
        if vmrun.is_running(source):
            raise VmwareMcpError(
                f"'{source.stem}' is powered on. Cloning requires it to be off -- call "
                "power_vm with action='stop' first."
            )

        parent = Path(directory) if directory else source.parent.parent
        ensure_allowed(parent.resolve() if parent.exists() else parent)

        target_dir = parent / new_name
        if target_dir.exists() and any(target_dir.iterdir()):
            raise VmwareMcpError(f"{target_dir} already exists and is not empty.")
        target_dir.mkdir(parents=True, exist_ok=True)
        target_vmx = target_dir / f"{new_name}.vmx"

        args = [str(source), str(target_vmx), clone_type, f"-cloneName={new_name}"]
        if snapshot:
            args.append(f"-snapshot={snapshot}")

        try:
            vmrun("clone", *args)
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

        return {
            "source": str(source),
            "clone": str(target_vmx),
            "name": new_name,
            "clone_type": clone_type,
            "from_snapshot": snapshot,
            "power_state": "poweredOff",
        }

    @mcp.tool(
        title="Delete virtual machine",
        annotations=DESTRUCTIVE,
        tags={"vmware", "lifecycle"},
    )
    @tool_errors
    def delete_vm(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        confirm_name: Annotated[
            str,
            Field(
                description="Type the VM's exact display name again to confirm. Guards "
                "against deleting the wrong VM when a fuzzy name matched."
            ),
        ],
    ) -> dict:
        """Permanently delete a VM and every file in its folder.

        Irreversible: disks, snapshots, and saved state are all destroyed.
        Requires VMWARE_MCP_ALLOW_DESTRUCTIVE=1 and the VM to be powered off.
        A linked clone of this VM would be broken by the deletion.
        """
        require_destructive_enabled("delete_vm")
        vmx_path = resolve_vmx(vm)

        if confirm_name.strip().lower() not in {
            vmx_path.stem.lower(),
            vmx_path.parent.name.lower(),
        }:
            raise VmwareMcpError(
                f"confirm_name '{confirm_name}' does not match the resolved VM "
                f"'{vmx_path.stem}' at {vmx_path}. Re-check which VM you meant, then pass "
                "its exact name."
            )

        vmrun = VmRun()
        if vmrun.is_running(vmx_path):
            raise VmwareMcpError(
                f"'{vmx_path.stem}' is powered on. Stop it first with power_vm."
            )

        directory = vmx_path.parent
        vmrun("deleteVM", str(vmx_path))

        # vmrun only removes files it recognises, so our own .vmx backups survive.
        for stale in backup_files(vmx_path):
            with contextlib.suppress(OSError):
                stale.unlink()
        with contextlib.suppress(OSError):
            directory.rmdir()  # only succeeds if nothing else is left

        leftovers = (
            [p.name for p in sorted(directory.iterdir())] if directory.exists() else []
        )
        return {
            "deleted": str(vmx_path),
            "directory": str(directory),
            "directory_still_exists": directory.exists(),
            "leftover_files": leftovers,
            "note": (
                "Some files remain in the folder; VMware only deletes what it owns. "
                "Remove them manually if they are not needed."
            )
            if leftovers
            else None,
        }
