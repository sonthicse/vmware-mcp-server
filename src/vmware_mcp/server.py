"""FastMCP server exposing VMware Workstation Pro to MCP clients."""

from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP

from .tools import register_all

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Controls VMware Workstation Pro on this Windows host: inventory, power state,
snapshots, virtual hardware, disks, networking, and guest-OS operations.

Identifying a VM: every tool takes a `vm` argument that accepts a display name
("Ubuntu Server 26.04 LTS"), a VM folder, or a full path to a .vmx file. Partial
names are resolved when they are unambiguous; call list_vms when they are not.

Two constraints shape most workflows:

1. Hardware edits (set_vm_hardware, set_vm_options, add_disk, attach_iso,
   set_network_adapter, ...) rewrite the .vmx file and are refused while the VM
   is powered on. Stop the VM first with power_vm.
2. Guest tools (guest_run_command, guest_copy_file, get_vm_ip) need the VM
   powered on with VMware Tools running, plus guest credentials.

Snapshots are the only undo available for most operations; create_snapshot is
cheap. Deleting VMs, disks, and snapshots is gated behind the
VMWARE_MCP_ALLOW_DESTRUCTIVE environment variable on the server.
"""


def build_server() -> FastMCP:
    mcp = FastMCP(
        name="vmware-workstation",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )
    register_all(mcp)
    return mcp


def main() -> None:
    # stdio transport owns stdout, so all logging must go to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_server().run()


if __name__ == "__main__":
    main()
