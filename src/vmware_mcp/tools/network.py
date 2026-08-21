"""Network adapter and host NAT tools."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..devices import list_nics, next_free_nic
from ..errors import VmwareMcpError
from ..inventory import load_vmx, require_offline
from ..vmrun import VmRun
from .base import DESTRUCTIVE, MUTATING, saved_result, tool_errors

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# Statically assigned MACs must sit in VMware's reserved 00:50:56 range.
_STATIC_MAC_PREFIX = "00:50:56"

CONNECTION_TYPES = {
    "bridged": "Adapter is bridged onto a physical host NIC; the guest gets an address "
    "from the physical LAN.",
    "nat": "Guest shares the host's IP via VMware NAT (VMnet8).",
    "hostonly": "Private network between host and guest only (VMnet1); no external access.",
    "custom": "Attach to a specific VMnet; requires the `vmnet` argument.",
    "none": "Adapter present but not connected to any network (LAN segment / disconnected).",
}


def _nic_prefix(adapter: str) -> str:
    adapter = adapter.strip().lower()
    if adapter.isdigit():
        return f"ethernet{adapter}"
    if re.fullmatch(r"ethernet\d+", adapter):
        return adapter
    raise VmwareMcpError(
        f"'{adapter}' is not a valid adapter. Use an index like '0' or a node name "
        "like 'ethernet0'. Call get_vm_info to see existing adapters."
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Configure network adapter",
        annotations=MUTATING,
        tags={"vmware", "config", "network"},
    )
    @tool_errors
    def set_network_adapter(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        adapter: Annotated[
            str,
            Field(description="Adapter to change: an index like '0' or a node like 'ethernet0'."),
        ] = "ethernet0",
        connection_type: Annotated[
            Literal["bridged", "nat", "hostonly", "custom", "none"] | None,
            Field(
                description="How the adapter attaches to the host. Use 'custom' together "
                "with `vmnet` to pin it to a specific VMnet."
            ),
        ] = None,
        vmnet: Annotated[
            str | None,
            Field(
                description="Target host network for connection_type='custom', e.g. "
                "'VMnet8'. See list_host_networks."
            ),
        ] = None,
        virtual_device: Annotated[
            Literal["e1000", "e1000e", "vmxnet3"] | None,
            Field(
                description="Emulated NIC model. vmxnet3 is fastest but needs VMware Tools "
                "drivers in the guest; e1000e is the safe default for modern Windows."
            ),
        ] = None,
        mac_address: Annotated[
            str | None,
            Field(
                description="Static MAC in the form 00:50:56:XX:YY:ZZ. VMware only accepts "
                "statically assigned MACs in the 00:50:56 range. Omit to keep the "
                "auto-generated address."
            ),
        ] = None,
        connect_at_power_on: Annotated[
            bool | None,
            Field(description="Whether the virtual cable is plugged in when the VM boots."),
        ] = None,
    ) -> dict:
        """Change an existing network adapter's connection type, model, or MAC.

        Requires the VM to be powered off. To add a new adapter use
        add_network_adapter; to see current adapters use get_vm_info.
        """
        prefix = _nic_prefix(adapter)
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Editing a network adapter")

        if not vmx.has(f"{prefix}.present"):
            existing = [n["node"] for n in list_nics(vmx)]
            raise VmwareMcpError(
                f"{vmx.path.stem} has no adapter '{prefix}'. Existing adapters: "
                f"{existing or 'none'}. Use add_network_adapter to create one."
            )

        if connection_type == "custom" and not vmnet:
            raise VmwareMcpError(
                "connection_type='custom' needs a `vmnet` such as 'VMnet8'. "
                "Call list_host_networks for the available names."
            )
        if vmnet and connection_type not in (None, "custom"):
            raise VmwareMcpError(
                f"`vmnet` only applies to connection_type='custom', not '{connection_type}'."
            )
        if mac_address and not _MAC_RE.match(mac_address):
            raise VmwareMcpError(
                f"'{mac_address}' is not a MAC address. Expected the form 00:50:56:12:34:56."
            )
        if mac_address and not mac_address.lower().startswith(_STATIC_MAC_PREFIX):
            raise VmwareMcpError(
                f"VMware only accepts static MACs starting with {_STATIC_MAC_PREFIX}. "
                f"Use something like {_STATIC_MAC_PREFIX}:12:34:56, or omit mac_address to "
                "keep the generated one."
            )

        changes: dict[str, Any] = {}

        if connection_type is not None:
            vmx.set(f"{prefix}.connectionType", connection_type)
            changes["connection_type"] = connection_type
            if connection_type == "custom":
                vmx.set(f"{prefix}.vnet", vmnet)
                vmx.set(f"{prefix}.displayName", vmnet)
                changes["vmnet"] = vmnet
            else:
                vmx.unset(f"{prefix}.vnet")
                vmx.unset(f"{prefix}.displayName")
        elif vmnet:
            vmx.set(f"{prefix}.vnet", vmnet)
            vmx.set(f"{prefix}.displayName", vmnet)
            changes["vmnet"] = vmnet

        if virtual_device is not None:
            vmx.set(f"{prefix}.virtualDev", virtual_device)
            changes["virtual_device"] = virtual_device

        if mac_address is not None:
            vmx.set(f"{prefix}.addressType", "static")
            vmx.set(f"{prefix}.address", mac_address.lower())
            vmx.unset(f"{prefix}.generatedAddress")
            vmx.unset(f"{prefix}.generatedAddressOffset")
            changes["mac_address"] = mac_address.lower()

        if connect_at_power_on is not None:
            vmx.set(f"{prefix}.startConnected", connect_at_power_on)
            changes["connect_at_power_on"] = connect_at_power_on

        if not changes:
            raise VmwareMcpError(
                "No changes supplied. Pass at least one of connection_type, vmnet, "
                "virtual_device, mac_address, connect_at_power_on."
            )

        backup = vmx.save()
        result = saved_result(vmx, changes, backup)
        result["adapter"] = prefix
        return result

    @mcp.tool(
        title="Add network adapter",
        annotations=MUTATING,
        tags={"vmware", "config", "network"},
    )
    @tool_errors
    def add_network_adapter(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        connection_type: Annotated[
            Literal["bridged", "nat", "hostonly", "custom", "none"],
            Field(description="How the new adapter attaches to the host."),
        ] = "nat",
        vmnet: Annotated[
            str | None,
            Field(description="Host network for connection_type='custom', e.g. 'VMnet2'."),
        ] = None,
        virtual_device: Annotated[
            Literal["e1000", "e1000e", "vmxnet3"],
            Field(description="Emulated NIC model."),
        ] = "e1000e",
    ) -> dict:
        """Add a new virtual network adapter to a powered-off VM.

        Picks the next free ethernetN slot (VMware allows up to 10). The MAC is
        auto-generated; set a static one afterwards with set_network_adapter.
        """
        if connection_type == "custom" and not vmnet:
            raise VmwareMcpError(
                "connection_type='custom' needs a `vmnet` such as 'VMnet2'. "
                "Call list_host_networks for available names."
            )

        vmx = load_vmx(vm)
        require_offline(vmx.path, "Adding a network adapter")

        try:
            prefix = next_free_nic(vmx)
        except ValueError as exc:
            raise VmwareMcpError(str(exc)) from exc

        vmx.update(
            {
                f"{prefix}.present": True,
                f"{prefix}.connectionType": connection_type,
                f"{prefix}.virtualDev": virtual_device,
                f"{prefix}.addressType": "generated",
                f"{prefix}.startConnected": True,
            }
        )
        if connection_type == "custom":
            vmx.set(f"{prefix}.vnet", vmnet)
            vmx.set(f"{prefix}.displayName", vmnet)

        backup = vmx.save()
        result = saved_result(
            vmx,
            {
                "adapter": prefix,
                "connection_type": connection_type,
                "vmnet": vmnet,
                "virtual_device": virtual_device,
            },
            backup,
        )
        result["adapter"] = prefix
        return result

    @mcp.tool(
        title="Remove network adapter",
        annotations=DESTRUCTIVE,
        tags={"vmware", "config", "network"},
    )
    @tool_errors
    def remove_network_adapter(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        adapter: Annotated[
            str, Field(description="Adapter to remove: index like '1' or node like 'ethernet1'.")
        ],
    ) -> dict:
        """Remove a network adapter and all of its .vmx keys.

        The guest OS will see the NIC disappear, which can strand static IP
        configuration. Requires the VM to be powered off.
        """
        prefix = _nic_prefix(adapter)
        vmx = load_vmx(vm)
        require_offline(vmx.path, "Removing a network adapter")

        if not vmx.has(f"{prefix}.present"):
            existing = [n["node"] for n in list_nics(vmx)]
            raise VmwareMcpError(
                f"{vmx.path.stem} has no adapter '{prefix}'. Existing adapters: {existing}."
            )

        removed = vmx.unset_prefix(f"{prefix}.")
        backup = vmx.save()
        result = saved_result(vmx, {"adapter": prefix, "removed_keys": removed}, backup)
        result["adapter"] = prefix
        return result

    @mcp.tool(
        title="Manage NAT port forwarding",
        annotations=MUTATING,
        tags={"vmware", "network", "host"},
    )
    @tool_errors
    def manage_port_forwarding(
        action: Annotated[
            Literal["set", "delete"],
            Field(description="'set' adds or updates a rule; 'delete' removes one."),
        ],
        host_port: Annotated[
            int, Field(description="Port on the host that receives the connection.", ge=1, le=65535)
        ],
        guest_ip: Annotated[
            str | None,
            Field(
                description="Guest IP to forward to, e.g. '192.168.163.128'. Required for "
                "action='set'; see get_vm_ip."
            ),
        ] = None,
        guest_port: Annotated[
            int | None,
            Field(description="Destination port inside the guest. Required for action='set'.", ge=1, le=65535),
        ] = None,
        protocol: Annotated[Literal["tcp", "udp"], Field(description="Transport protocol.")] = "tcp",
        vmnet: Annotated[
            str, Field(description="NAT host network, normally 'VMnet8'.")
        ] = "VMnet8",
        description: Annotated[
            str | None, Field(description="Label shown in the VMware NAT settings UI.")
        ] = None,
    ) -> dict:
        """Add, update, or remove a NAT port forwarding rule on a host network.

        This is host-wide, not per-VM: the rule affects every VM on that NAT
        network, and it survives the VM being deleted. See list_port_forwardings
        for existing rules. Changing host networking may prompt for Windows
        administrator elevation.
        """
        vmrun = VmRun()
        if action == "set":
            if guest_ip is None or guest_port is None:
                raise VmwareMcpError(
                    "action='set' needs both guest_ip and guest_port. Use get_vm_ip to find "
                    "the guest's address."
                )
            args = [vmnet, protocol, str(host_port), guest_ip, str(guest_port)]
            if description:
                args.append(description)
            vmrun("setPortForwarding", *args)
            return {
                "action": "set",
                "vmnet": vmnet,
                "protocol": protocol,
                "host_port": host_port,
                "guest": f"{guest_ip}:{guest_port}",
            }

        vmrun("deletePortForwarding", vmnet, protocol, str(host_port))
        return {"action": "delete", "vmnet": vmnet, "protocol": protocol, "host_port": host_port}
