"""Tools that edit a VM's virtual hardware and general options."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..errors import VmwareMcpError
from ..inventory import load_vmx, require_offline
from .base import MUTATING, saved_result, tool_errors

# Keys we refuse to rewrite through the generic escape hatch: changing them
# breaks the VM's identity or the safety guarantees this server relies on.
PROTECTED_KEYS = {
    "uuid.bios",
    "uuid.location",
    "vmx.buildtype",
    ".encoding",
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Configure CPU, memory, and display",
        annotations=MUTATING,
        tags={"vmware", "config"},
    )
    @tool_errors
    def set_vm_hardware(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        cpus: Annotated[
            int | None,
            Field(description="Total virtual processors (numvcpus).", ge=1, le=128),
        ] = None,
        cores_per_socket: Annotated[
            int | None,
            Field(
                description="Cores per socket. Must divide `cpus` evenly; sockets are "
                "derived as cpus / cores_per_socket.",
                ge=1,
                le=64,
            ),
        ] = None,
        memory_mb: Annotated[
            int | None,
            Field(
                description="RAM in megabytes (memsize). VMware requires a multiple of 4.",
                ge=32,
                le=1024 * 1024,
            ),
        ] = None,
        firmware: Annotated[
            Literal["bios", "efi"] | None,
            Field(
                description="Boot firmware. Switching this on an installed guest will "
                "usually make it unbootable -- only change it before OS installation."
            ),
        ] = None,
        secure_boot: Annotated[
            bool | None,
            Field(description="UEFI Secure Boot. Only meaningful when firmware='efi'."),
        ] = None,
        virtualize_vtx: Annotated[
            bool | None,
            Field(
                description="Expose Intel VT-x/AMD-V to the guest (vhv.enable). Needed to "
                "run nested hypervisors, WSL2, or Hyper-V inside the VM."
            ),
        ] = None,
        virtualize_iommu: Annotated[
            bool | None,
            Field(description="Expose an IOMMU to the guest (vvtd.enable)."),
        ] = None,
        accelerate_3d: Annotated[
            bool | None,
            Field(description="Enable 3D graphics acceleration (mks.enable3d)."),
        ] = None,
        graphics_memory_mb: Annotated[
            int | None,
            Field(
                description="Graphics memory in MB (svga.graphicsMemoryKB). Typical values "
                "are 1024-8192.",
                ge=16,
                le=32768,
            ),
        ] = None,
        num_displays: Annotated[
            int | None,
            Field(description="Number of monitors to present to the guest.", ge=1, le=8),
        ] = None,
        hardware_version: Annotated[
            int | None,
            Field(
                description="Virtual hardware version (virtualHW.version). Workstation 17 "
                "supports up to 21. Lowering it below the current value is not supported.",
                ge=4,
                le=21,
            ),
        ] = None,
    ) -> dict:
        """Set CPU, memory, firmware, and display settings on a powered-off VM.

        Every parameter is optional -- omitted ones are left untouched. The VM
        must be powered off; call power_vm with action='stop' first. For settings
        this tool does not model, use set_vm_config.
        """
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Editing virtual hardware")

        current_cpus = cpus if cpus is not None else (vmx.get_int("numvcpus", 1) or 1)
        if cores_per_socket is not None and current_cpus % cores_per_socket != 0:
            raise VmwareMcpError(
                f"cores_per_socket={cores_per_socket} does not divide cpus={current_cpus} "
                "evenly. Pick a value such as "
                f"{[c for c in range(1, current_cpus + 1) if current_cpus % c == 0]}."
            )
        if memory_mb is not None and memory_mb % 4 != 0:
            raise VmwareMcpError(
                f"memory_mb={memory_mb} is not a multiple of 4. VMware rejects other "
                f"values; try {memory_mb - memory_mb % 4}."
            )

        changes: dict[str, Any] = {}

        def apply(key: str, value: Any, label: str) -> None:
            if value is None:
                return
            vmx.set(key, value)
            changes[label] = value

        apply("numvcpus", cpus, "cpus")
        apply("cpuid.coresPerSocket", cores_per_socket, "cores_per_socket")
        apply("memsize", memory_mb, "memory_mb")
        apply("firmware", firmware, "firmware")
        apply("uefi.secureBoot.enabled", secure_boot, "secure_boot")
        apply("vhv.enable", virtualize_vtx, "virtualize_vtx")
        apply("vvtd.enable", virtualize_iommu, "virtualize_iommu")
        apply("mks.enable3d", accelerate_3d, "accelerate_3d")
        apply("svga.numDisplays", num_displays, "num_displays")
        apply("virtualHW.version", hardware_version, "hardware_version")

        if graphics_memory_mb is not None:
            vmx.set("svga.graphicsMemoryKB", graphics_memory_mb * 1024)
            vmx.set("svga.vramSize", graphics_memory_mb * 1024 * 1024)
            changes["graphics_memory_mb"] = graphics_memory_mb
        if num_displays is not None:
            vmx.set("svga.autodetect", False)

        if not changes:
            raise VmwareMcpError(
                "No settings supplied. Pass at least one of cpus, cores_per_socket, "
                "memory_mb, firmware, secure_boot, virtualize_vtx, virtualize_iommu, "
                "accelerate_3d, graphics_memory_mb, num_displays, hardware_version."
            )

        backup = vmx.save()
        return saved_result(vmx, changes, backup)

    @mcp.tool(
        title="Configure VM options",
        annotations=MUTATING,
        tags={"vmware", "config"},
    )
    @tool_errors
    def set_vm_options(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        display_name: Annotated[
            str | None,
            Field(description="Name shown in the Workstation library.", max_length=120),
        ] = None,
        guest_os: Annotated[
            str | None,
            Field(
                description="VMware guest OS identifier, e.g. 'windows11-64', 'ubuntu-64', "
                "'debian12-64', 'rhel9-64', 'centos9-64', 'otherlinux-64', 'other-64'. "
                "Wrong values only affect defaults and VMware Tools selection."
            ),
        ] = None,
        annotation: Annotated[
            str | None,
            Field(description="Free-text notes shown on the VM's summary tab."),
        ] = None,
        sync_time_with_host: Annotated[
            bool | None,
            Field(description="Let VMware Tools keep the guest clock synced to the host."),
        ] = None,
        shared_folders_enabled: Annotated[
            bool | None,
            Field(description="Enable the host-guest shared folders (HGFS) feature."),
        ] = None,
        power_off_type: Annotated[
            Literal["soft", "hard"] | None,
            Field(
                description="What the Workstation 'Power Off' button does: 'soft' asks the "
                "guest to shut down, 'hard' cuts power."
            ),
        ] = None,
        enable_drag_and_drop: Annotated[
            bool | None, Field(description="Allow drag-and-drop between host and guest.")
        ] = None,
        enable_clipboard: Annotated[
            bool | None, Field(description="Allow copy/paste between host and guest.")
        ] = None,
    ) -> dict:
        """Set a VM's name, guest OS type, notes, and host-integration options.

        Every parameter is optional. Requires the VM to be powered off.
        Note: display_name changes the library label only -- it does not rename
        the .vmx file or its folder.
        """
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Editing VM options")

        changes: dict[str, Any] = {}

        def apply(key: str, value: Any, label: str, invert: bool = False) -> None:
            if value is None:
                return
            vmx.set(key, (not value) if invert else value)
            changes[label] = value

        apply("displayName", display_name, "display_name")
        apply("guestOS", guest_os, "guest_os")
        apply("annotation", annotation, "annotation")
        apply("tools.syncTime", sync_time_with_host, "sync_time_with_host")
        # VMware stores these as "disable" flags, so the value is inverted.
        apply("isolation.tools.hgfs.disable", shared_folders_enabled, "shared_folders_enabled", True)
        apply("isolation.tools.dnd.disable", enable_drag_and_drop, "enable_drag_and_drop", True)
        if enable_clipboard is not None:
            vmx.set("isolation.tools.copy.disable", not enable_clipboard)
            vmx.set("isolation.tools.paste.disable", not enable_clipboard)
            changes["enable_clipboard"] = enable_clipboard
        if power_off_type is not None:
            vmx.set("powerType.powerOff", power_off_type)
            changes["power_off_type"] = power_off_type

        if not changes:
            raise VmwareMcpError(
                "No options supplied. Pass at least one of display_name, guest_os, "
                "annotation, sync_time_with_host, shared_folders_enabled, power_off_type, "
                "enable_drag_and_drop, enable_clipboard."
            )

        backup = vmx.save()
        return saved_result(vmx, changes, backup)

    @mcp.tool(
        title="Write raw .vmx settings",
        annotations=MUTATING,
        tags={"vmware", "config", "advanced"},
    )
    @tool_errors
    def set_vm_config(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        settings: Annotated[
            dict[str, str],
            Field(
                description="Map of .vmx keys to values, e.g. "
                '{"vhv.enable": "TRUE", "mainMem.useNamedFile": "FALSE"}. Values are '
                "written verbatim; booleans must be the strings TRUE/FALSE."
            ),
        ],
    ) -> dict:
        """Write arbitrary key/value pairs into a VM's .vmx file.

        The escape hatch for settings the typed tools do not cover. Prefer
        set_vm_hardware / set_vm_options / set_network_adapter where they apply --
        they validate input. Existing keys are updated in place, new ones
        appended, and a timestamped .bak of the .vmx is kept.
        """
        if not settings:
            raise VmwareMcpError("`settings` was empty -- nothing to write.")

        blocked = [k for k in settings if k.strip().lower() in PROTECTED_KEYS]
        if blocked:
            raise VmwareMcpError(
                f"Refusing to modify protected keys: {', '.join(blocked)}. These define "
                "the VM's identity and encoding; changing them can detach snapshots or "
                "corrupt the file."
            )

        vmx = load_vmx(vm)
        require_offline(vmx.path, "Editing .vmx settings")

        changes = {}
        for key, value in settings.items():
            changes[key] = {"old": vmx.get(key), "new": value}
            vmx.set(key, value)

        backup = vmx.save()
        return saved_result(vmx, changes, backup)

    @mcp.tool(
        title="Remove raw .vmx settings",
        annotations=MUTATING,
        tags={"vmware", "config", "advanced"},
    )
    @tool_errors
    def unset_vm_config(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        keys: Annotated[
            list[str],
            Field(
                description="Exact .vmx keys to delete. Removing a key restores VMware's "
                "built-in default for that setting.",
                min_length=1,
            ),
        ],
    ) -> dict:
        """Delete keys from a VM's .vmx file, reverting them to VMware's defaults.

        Use after set_vm_config to undo an experiment. To remove a device use the
        dedicated tool (detach_disk, remove_network_adapter) instead -- those
        clear every related key.
        """
        blocked = [k for k in keys if k.strip().lower() in PROTECTED_KEYS]
        if blocked:
            raise VmwareMcpError(
                f"Refusing to remove protected keys: {', '.join(blocked)}."
            )

        vmx = load_vmx(vm)
        require_offline(vmx.path, "Editing .vmx settings")

        removed, missing = [], []
        for key in keys:
            (removed if vmx.unset(key) else missing).append(key)

        if not removed:
            raise VmwareMcpError(
                f"None of those keys are present in {vmx.path.name}: {', '.join(missing)}. "
                "Call get_vm_config to see the actual keys."
            )

        backup = vmx.save()
        return saved_result(vmx, {"removed": removed, "not_present": missing}, backup)
