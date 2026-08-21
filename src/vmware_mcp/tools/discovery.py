"""Read-only tools: inventory, configuration, host networking."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ..config import get_config
from ..inventory import discover_vms, load_vmx, resolve_vmx
from ..vmrun import VmRun
from .base import READ_ONLY, tool_errors, vm_summary


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="List virtual machines",
        annotations=READ_ONLY,
        tags={"vmware", "read"},
    )
    @tool_errors
    def list_vms(
        running_only: Annotated[
            bool, Field(description="Return only VMs that are currently powered on.")
        ] = False,
    ) -> dict:
        """List every VM known to VMware Workstation with its power state.

        Sources are VMware's own library (inventory.vmls) plus a scan of the
        configured VM directories. Returns names and .vmx paths -- pass either
        back to any other tool as the `vm` argument. For full hardware details of
        one VM use get_vm_info instead.
        """
        vmrun = VmRun()
        running = {str(p).lower() for p in vmrun.running_vms()}

        vms = []
        for ref in discover_vms():
            entry = ref.to_dict()
            entry["power_state"] = (
                "poweredOn" if str(ref.vmx_path).lower() in running else "poweredOff"
            )
            if running_only and entry["power_state"] != "poweredOn":
                continue
            vms.append(entry)

        return {
            "count": len(vms),
            "running_count": sum(1 for v in vms if v["power_state"] == "poweredOn"),
            "vms": vms,
            "searched_directories": [str(d) for d in get_config().vm_dirs],
        }

    @mcp.tool(
        title="Get virtual machine details",
        annotations=READ_ONLY,
        tags={"vmware", "read"},
    )
    @tool_errors
    def get_vm_info(
        vm: Annotated[
            str, Field(description="VM display name, folder, or full path to its .vmx file.")
        ],
        include_disk_details: Annotated[
            bool,
            Field(
                description="Read each .vmdk to report capacity and space used. "
                "Slightly slower; set false for a quick summary."
            ),
        ] = True,
    ) -> dict:
        """Report a VM's power state and full hardware configuration.

        Covers CPU, memory, firmware, display, disks, CD-ROMs, and network
        adapters. This is the interpreted view -- for raw .vmx keys (including
        ones this server does not model) use get_vm_config.
        """
        vmx = load_vmx(vm)
        return vm_summary(vmx, with_disks=include_disk_details)

    @mcp.tool(
        title="Read raw .vmx settings",
        annotations=READ_ONLY,
        tags={"vmware", "read"},
    )
    @tool_errors
    def get_vm_config(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        prefix: Annotated[
            str | None,
            Field(
                description="Only return keys starting with this prefix, e.g. 'ethernet0' "
                "or 'scsi0'. Case-insensitive. Omit for every key."
            ),
        ] = None,
    ) -> dict:
        """Return raw key/value pairs from the VM's .vmx file.

        Use this to inspect advanced or undocumented settings that get_vm_info
        does not model. Write them back with set_vm_config.
        """
        vmx = load_vmx(vm)
        entries = vmx.keys_with_prefix(prefix) if prefix else vmx.as_dict()
        duplicates = vmx.duplicate_keys()
        return {
            "vmx_path": str(vmx.path),
            "prefix": prefix,
            "count": len(entries),
            "settings": entries,
            # `settings` collapses repeats to the last value; list them so a
            # corrupt file is visible rather than silently flattened.
            "duplicate_keys": duplicates or None,
        }

    @mcp.tool(
        title="List host virtual networks",
        annotations=READ_ONLY,
        tags={"vmware", "read", "network"},
    )
    @tool_errors
    def list_host_networks() -> dict:
        """List the host's virtual networks (vmnet0, vmnet1, vmnet8, ...).

        Shows each network's type (bridged/nat/hostOnly), whether its DHCP server
        is on, and its subnet. Use the names returned here as the `vmnet`
        argument of set_network_adapter and manage_port_forwarding.
        """
        raw = VmRun()("listHostNetworks")
        networks = []
        # Columns are: INDEX  NAME  TYPE  DHCP  SUBNET  MASK
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            networks.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "type": parts[2],
                    "dhcp": parts[3].lower() == "true" if len(parts) > 3 else None,
                    "subnet": parts[4] if len(parts) > 4 else None,
                    "mask": parts[5] if len(parts) > 5 else None,
                }
            )
        return {"count": len(networks), "networks": networks, "raw": raw}

    @mcp.tool(
        title="List NAT port forwardings",
        annotations=READ_ONLY,
        tags={"vmware", "read", "network"},
    )
    @tool_errors
    def list_port_forwardings(
        vmnet: Annotated[
            str, Field(description="Host network name, e.g. 'VMnet8' (the default NAT network).")
        ] = "VMnet8",
    ) -> dict:
        """List port forwardings configured on a NAT host network.

        Only NAT networks support forwarding. Change them with set_port_forwarding.
        """
        raw = VmRun()("listPortForwardings", vmnet)
        return {"vmnet": vmnet, "raw": raw}

    @mcp.tool(
        title="Get guest IP address",
        annotations=READ_ONLY,
        tags={"vmware", "read", "guest"},
    )
    @tool_errors
    def get_vm_ip(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        wait: Annotated[
            bool,
            Field(
                description="Block until the guest reports an address. Use after "
                "powering on a VM that is still booting."
            ),
        ] = False,
    ) -> dict:
        """Get the primary IP address the guest OS reports to VMware Tools.

        Requires the VM to be powered on with VMware Tools running. Fails with a
        timeout error if Tools is not installed.
        """
        vmx_path = resolve_vmx(vm)
        args = [str(vmx_path)]
        if wait:
            args.append("-wait")
        ip = VmRun()("getGuestIPAddress", *args).strip()
        return {"vm": vmx_path.stem, "ip_address": ip}

    @mcp.tool(
        title="Check VMware Tools status",
        annotations=READ_ONLY,
        tags={"vmware", "read", "guest"},
    )
    @tool_errors
    def check_vmware_tools(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
    ) -> dict:
        """Report whether VMware Tools is installed and running in the guest.

        Guest tools (guest_run_command, guest_copy_file, get_vm_ip) only work
        when this returns 'running'.
        """
        vmx_path = resolve_vmx(vm)
        state = VmRun()("checkToolsState", str(vmx_path)).strip()
        return {"vm": vmx_path.stem, "tools_state": state}

    @mcp.tool(
        title="Show server configuration",
        annotations=READ_ONLY,
        tags={"vmware", "read", "diagnostics"},
    )
    @tool_errors
    def get_server_config() -> dict:
        """Report how this MCP server is configured on the host.

        Useful when a tool fails with a path or permission error: it shows the
        VMware install location, which directories VMs may live in, which are
        approved for host/guest file transfer, and whether destructive
        operations are enabled.
        """
        config = get_config()
        return {
            "vmware_install_dir": str(config.install_dir),
            "vmrun": str(config.vmrun),
            "vmrun_available": config.vmrun.is_file(),
            "vdiskmanager_available": config.vdiskmanager.is_file(),
            "allowed_vm_directories": [str(d) for d in config.vm_dirs],
            "default_vm_directory": str(config.default_vm_dir),
            "allow_any_path": config.allow_any_path,
            "allow_destructive": config.allow_destructive,
            "host_io_directories": [str(d) for d in config.host_io_dirs],
            "host_guest_file_transfer_enabled": bool(config.host_io_dirs),
            "guest_credentials_configured": bool(config.guest_user),
            "command_timeout_seconds": config.command_timeout,
        }
