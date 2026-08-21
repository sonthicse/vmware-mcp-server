# vmware-mcp-server

An MCP server that lets Claude Code configure and drive **VMware Workstation Pro** on Windows.

It talks to Workstation through `vmrun.exe` and `vmware-vdiskmanager.exe`, and edits `.vmx`
files directly for the settings the CLI cannot reach (CPU topology, firmware, NIC models,
display memory, and so on).

## Requirements

- Windows with VMware Workstation Pro (or Player) — tested against Workstation 17.6, `vmrun` 1.17.0
- Python 3.10+
- `fastmcp >= 3.0`

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Register with Claude Code

The repo ships a project-scoped `.mcp.json`, so from the project directory Claude Code
picks the server up automatically. To register it globally instead:

```powershell
claude mcp add vmware --scope user -- "C:\path\to\vmware-mcp-server\.venv\Scripts\python.exe" "C:\path\to\vmware-mcp-server\main.py"
```

Verify with `/mcp` inside Claude Code, or from a shell:

```powershell
.\.venv\Scripts\python.exe main.py   # should sit waiting on stdio
```

## Configuration

All settings are environment variables read once at start-up.

| Variable | Default | Purpose |
|---|---|---|
| `VMWARE_MCP_INSTALL_DIR` | auto-detected | Folder containing `vmrun.exe`. Set it if detection fails. |
| `VMWARE_MCP_VM_DIRS` | VMware's usual VM folders | Semicolon-separated directories VMs may live in. **This is the safety boundary** — the server refuses to read or write `.vmx` files outside it. |
| `VMWARE_MCP_ALLOW_ANY_PATH` | `0` | Set to `1` to disable the VM directory check entirely. Does **not** affect host/guest file transfer. |
| `VMWARE_MCP_HOST_IO_DIRS` | *empty* | Directories that `guest_copy_file` and `set_shared_folder` may touch on the host. **Empty means those two tools are disabled.** Point it at a dedicated exchange folder — never a VM directory, source tree, or user profile, since the guest controls what lands there. |
| `VMWARE_MCP_ALLOW_DESTRUCTIVE` | `0` | Set to `1` to unlock `delete_vm`, `delete_snapshot`, and deleting `.vmdk` files. |
| `VMWARE_MCP_GUEST_USER` / `VMWARE_MCP_GUEST_PASSWORD` | unset | Default credentials for guest-OS tools. Can also be passed per call. |
| `VMWARE_MCP_TIMEOUT` | `300` | Seconds before a VMware command is abandoned. Raise for large clones or disk conversions. |

Install detection order: `VMWARE_MCP_INSTALL_DIR` → Windows registry → the standard
`Program Files` locations.

## Design

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full architecture: layering, request
lifecycle, the `.vmx` write path, the safety model, and how to add a tool.

Three rules shape every tool:

1. **Names, not paths.** Every tool takes a `vm` argument accepting a display name
   (`"Ubuntu Server 26.04 LTS"`), an unambiguous fragment (`"Ubuntu"`), a VM folder, or a
   full `.vmx` path. Ambiguous names produce an error that lists the candidates.
2. **Offline edits are enforced, not hoped for.** VMware silently discards `.vmx` changes
   made to a running VM, so every hardware tool checks the power state first and tells you
   to stop the VM.
3. **Writes are reversible where possible.** Each `.vmx` rewrite goes through a temp file
   and leaves a timestamped `.bak` (the five most recent are kept). Genuinely destructive
   operations are gated behind `VMWARE_MCP_ALLOW_DESTRUCTIVE`.
4. **The guest is not trusted.** Anything crossing the host/guest boundary needs an
   explicitly nominated directory (`VMWARE_MCP_HOST_IO_DIRS`, empty by default), and no
   free-text parameter can inject settings into a `.vmx` — the serializer refuses quotes
   and line breaks outright, because the format cannot escape them.
5. **A `.vmx` VMware cannot open is never written.** Workstation refuses to load a file
   with a repeated key (verified against 17.6), so duplicates are treated as corruption:
   they are reported by `get_vm_info`, repaired when you write that key, and block a save
   otherwise.

## Tools

**Discovery (read-only)** — `list_vms`, `get_vm_info`, `get_vm_config`, `list_host_networks`,
`list_port_forwardings`, `get_vm_ip`, `check_vmware_tools`, `get_server_config`

**Power & snapshots** — `power_vm`, `list_snapshots`, `create_snapshot`, `revert_snapshot`,
`delete_snapshot`

**Hardware & options** — `set_vm_hardware` (CPU, RAM, firmware, Secure Boot, VT-x
passthrough, 3D, graphics memory, HW version), `set_vm_options` (name, guest OS, notes,
clipboard, drag-and-drop, shared folders), `set_vm_config` / `unset_vm_config` (raw `.vmx`
escape hatch)

**Networking** — `set_network_adapter`, `add_network_adapter`, `remove_network_adapter`,
`manage_port_forwarding`

**Storage & media** — `add_disk`, `detach_disk`, `resize_disk`, `optimize_disk`,
`attach_iso`, `detach_iso`

**Lifecycle** — `create_vm`, `clone_vm`, `delete_vm`

**Guest OS** (needs VMware Tools + credentials) — `guest_run_command`, `guest_copy_file`†,
`guest_list_processes`, `guest_kill_process`, `set_shared_folder`†, `capture_screen`

† Additionally needs `VMWARE_MCP_HOST_IO_DIRS` set; disabled otherwise.
`capture_screen` writes to the system temp folder by default; an explicit `output_path`
must end in `.png`, must not already exist, and must be inside a configured VM directory.

## Example prompts

- "Show me every VM and which ones are running."
- "Give the Ubuntu VM 4 CPUs and 8 GB of RAM, and turn on VT-x passthrough."
- "Snapshot the Kali VM as 'clean', then put its NIC on vmnet2."
- "Create a Windows 11 VM with 8 GB RAM, a 120 GB NVMe disk, UEFI, and mount D:\iso\win11.iso."
- "Add a second 100 GB disk to the Ubuntu VM and grow its boot disk to 80 GB."
- "Forward host port 2222 to port 22 on the Ubuntu guest."

## Known limits

- **No vTPM.** Workstation requires VM encryption for a virtual TPM, which this server does
  not manage. A stock Windows 11 installer therefore needs its TPM check bypassed, or the
  vTPM added from the Workstation UI.
- **Disks only grow.** VMware cannot shrink a `.vmdk`; `resize_disk` also refuses while
  snapshots exist, because the chain cannot be expanded.
- **No exit codes from the guest.** `vmrun` does not return a guest command's exit status,
  so `guest_run_command` reports captured output only.
- **Host networking may prompt for elevation.** `manage_port_forwarding` changes host-wide
  VMware settings and can raise a Windows UAC dialog.
- **`delete_vm` leaves foreign files.** `vmrun deleteVM` removes only files VMware owns; any
  leftovers are reported back in the result.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests main.py evaluation
```

Tests cover the `.vmx` parser (encoding, CRLF, ordering, backup rotation), VM name
resolution, the allowed-directory gate, and device-slot allocation. They use temporary
directories and never touch real VMs.

### Tool-use evaluation

`tests/` checks that the tools work; `evaluation/` checks that a model can *use* them.
Ten questions in `evaluation/tool_use_eval.xml` target the mistakes a 36-tool surface
invites — reaching for the raw `.vmx` escape hatch when a typed tool exists, powering a
VM on to read a setting the file already holds, or treating a refusal from the
destructive gate as an obstacle to work around.

```powershell
.\.venv\Scripts\python.exe evaluation\run_evaluation.py list
.\.venv\Scripts\python.exe evaluation\run_evaluation.py validate
.\.venv\Scripts\python.exe evaluation\run_evaluation.py score transcript.json
```

Tool selection, ordering and arguments are graded mechanically; the rubric criteria are
printed as a checklist for a human or judge model. See [evaluation/README.md](evaluation/README.md)
for the fixture, the transcript format, and how to add a question.
