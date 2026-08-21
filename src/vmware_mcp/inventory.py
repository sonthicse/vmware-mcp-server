"""VM discovery, name resolution, and the path safety gate.

Every tool takes a free-form ``vm`` identifier -- a display name, a folder, or a
full ``.vmx`` path -- and funnels it through :func:`resolve_vmx`, which is also
where the allowed-directory check happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, get_config
from .errors import (
    AmbiguousVmError,
    HostIoNotAllowedError,
    PathNotAllowedError,
    VmNotFoundError,
    VmPoweredOnError,
)
from .vmrun import VmRun
from .vmx import VmxFile

_INVENTORY_RE = re.compile(r'^(?P<key>vmlist\d+\.\w+)\s*=\s*"(?P<value>.*)"\s*$', re.IGNORECASE)

MAX_SCAN_DEPTH = 4


@dataclass
class VmRef:
    name: str
    vmx_path: Path
    in_inventory: bool = False
    favorite: bool = False
    sources: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "vmx_path": str(self.vmx_path),
            "directory": str(self.vmx_path.parent),
            "in_inventory": self.in_inventory,
            "favorite": self.favorite,
            "exists": self.vmx_path.is_file(),
        }


# --------------------------------------------------------------------- scan


def parse_inventory(config: Config | None = None) -> list[VmRef]:
    """Read VMware's own ``inventory.vmls`` (the library sidebar)."""
    config = config or get_config()
    path = config.inventory_file
    if not path.is_file():
        return []

    try:
        raw = path.read_bytes().decode("cp1252", "replace")
    except OSError:
        return []

    grouped: dict[str, dict[str, str]] = {}
    for line in raw.splitlines():
        match = _INVENTORY_RE.match(line.strip())
        if not match:
            continue
        key = match.group("key")
        entry, _, prop = key.partition(".")
        grouped.setdefault(entry.lower(), {})[prop.lower()] = match.group("value")

    refs: list[VmRef] = []
    for props in grouped.values():
        config_path = props.get("config", "").strip()
        if not config_path.lower().endswith(".vmx"):
            continue
        vmx_path = Path(config_path)
        refs.append(
            VmRef(
                name=props.get("displayname") or vmx_path.stem,
                vmx_path=vmx_path,
                in_inventory=True,
                favorite=props.get("isfavorite", "").upper() == "TRUE",
                sources={"inventory"},
            )
        )
    return refs


def scan_vm_dirs(config: Config | None = None) -> list[VmRef]:
    """Find ``.vmx`` files under the configured VM directories."""
    config = config or get_config()
    refs: list[VmRef] = []
    for root in config.vm_dirs:
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        for vmx_path in root.rglob("*.vmx"):
            if len(vmx_path.parts) - root_depth > MAX_SCAN_DEPTH:
                continue
            refs.append(VmRef(name=vmx_path.stem, vmx_path=vmx_path, sources={"scan"}))
    return refs


def discover_vms(config: Config | None = None) -> list[VmRef]:
    """Union of the VMware library and a directory scan, de-duplicated by path.

    Library entries outside the allowed directories are dropped: surfacing a VM
    that every other tool would then refuse to touch only creates false
    ambiguity when resolving names.
    """
    config = config or get_config()
    merged: dict[str, VmRef] = {}
    candidates = [
        ref for ref in parse_inventory(config) if is_allowed(ref.vmx_path, config)
    ]
    for ref in [*candidates, *scan_vm_dirs(config)]:
        key = str(ref.vmx_path).lower().replace("/", "\\")
        existing = merged.get(key)
        if existing is None:
            merged[key] = ref
            continue
        existing.in_inventory = existing.in_inventory or ref.in_inventory
        existing.favorite = existing.favorite or ref.favorite
        existing.sources |= ref.sources
        if ref.in_inventory:
            existing.name = ref.name
    return sorted(merged.values(), key=lambda r: r.name.lower())


# ------------------------------------------------------------- path safety


def _within_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    """True when *path* resolves to somewhere under one of *roots*.

    Resolving first is what makes this immune to '..', symlinks, and mixed case;
    comparing with relative_to is per path component, so a sibling directory
    sharing a prefix (C:\\VMs-evil vs C:\\VMs) does not match. Fails closed.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        return True
    return False


def is_allowed(path: Path, config: Config | None = None) -> bool:
    config = config or get_config()
    if config.allow_any_path:
        return True
    return _within_roots(path, config.vm_dirs)


def ensure_allowed(path: Path, config: Config | None = None) -> Path:
    """Raise unless *path* sits inside a configured VM directory."""
    config = config or get_config()
    if is_allowed(path, config):
        return path
    listed = "\n".join(f"  - {d}" for d in config.vm_dirs) or "  (none configured)"
    raise PathNotAllowedError(
        f"{path} is outside the allowed VM directories:\n{listed}\n"
        "Add its parent folder to VMWARE_MCP_VM_DIRS (semicolon-separated), or set "
        "VMWARE_MCP_ALLOW_ANY_PATH=1 to disable this check entirely."
    )


def ensure_host_io_allowed(path: Path, operation: str, config: Config | None = None) -> Path:
    """Gate a host path that a guest OS will read from or write to.

    Guest file traffic gets its own allow-list, empty by default, so these tools
    are inert until an operator deliberately nominates a directory. It is
    intentionally not satisfied by VMWARE_MCP_VM_DIRS or VMWARE_MCP_ALLOW_ANY_PATH:
    those govern managing VMs, which is a different trust decision from handing an
    untrusted guest a path on the host.
    """
    config = config or get_config()
    if not config.host_io_dirs:
        raise HostIoNotAllowedError(
            f"{operation} moves data across the host/guest boundary, and no host "
            "directory is approved for that. It is disabled by default because the "
            "guest OS controls the content being written (or receives whatever is "
            "read). To enable it, set VMWARE_MCP_HOST_IO_DIRS to a dedicated exchange "
            r'folder — for example "C:\VM-Exchange" — in the server environment. Do '
            "not point it at a VM directory, a source tree, or a user profile."
        )
    if _within_roots(path, config.host_io_dirs):
        return path

    listed = "\n".join(f"  - {d}" for d in config.host_io_dirs)
    raise HostIoNotAllowedError(
        f"{path} is outside the directories approved for host/guest file transfer:\n"
        f"{listed}\nUse a path inside one of them, or add another to "
        "VMWARE_MCP_HOST_IO_DIRS (semicolon-separated)."
    )


# --------------------------------------------------------------- resolution


def _candidates_message(candidates: list[VmRef]) -> str:
    return "\n".join(f"  - {c.name}  ({c.vmx_path})" for c in candidates[:20])


def resolve_vmx(vm: str, config: Config | None = None) -> Path:
    """Turn a display name / directory / ``.vmx`` path into a validated path."""
    config = config or get_config()
    raw = vm.strip().strip('"')
    if not raw:
        raise VmNotFoundError("No VM specified. Call list_vms to see available VMs.")

    candidate = Path(raw)

    if raw.lower().endswith(".vmx"):
        if not candidate.is_file():
            raise VmNotFoundError(
                f"No .vmx file at {candidate}. Call list_vms to see available VMs."
            )
        return ensure_allowed(candidate.resolve(), config)

    if candidate.is_dir():
        found = sorted(candidate.glob("*.vmx"))
        if len(found) == 1:
            return ensure_allowed(found[0].resolve(), config)
        if not found:
            raise VmNotFoundError(f"No .vmx file inside the folder {candidate}.")
        listing = "\n".join(f"  - {p}" for p in found)
        raise AmbiguousVmError(
            f"The folder {candidate} holds several VMs:\n{listing}\n"
            "Pass the full .vmx path."
        )

    vms = discover_vms(config)
    lowered = raw.lower()

    exact = [v for v in vms if v.name.lower() == lowered or v.vmx_path.stem.lower() == lowered]
    if len(exact) == 1:
        return ensure_allowed(exact[0].vmx_path.resolve(), config)
    if len(exact) > 1:
        raise AmbiguousVmError(
            f"'{vm}' matches several VMs:\n{_candidates_message(exact)}\n"
            "Pass the full .vmx path."
        )

    partial = [v for v in vms if lowered in v.name.lower() or lowered in v.vmx_path.stem.lower()]
    if len(partial) == 1:
        return ensure_allowed(partial[0].vmx_path.resolve(), config)
    if len(partial) > 1:
        raise AmbiguousVmError(
            f"'{vm}' matches several VMs:\n{_candidates_message(partial)}\n"
            "Use the exact name or the full .vmx path."
        )

    known = _candidates_message(vms) or "  (none found)"
    raise VmNotFoundError(f"No VM matching '{vm}'. Known VMs:\n{known}")


def load_vmx(vm: str, config: Config | None = None) -> VmxFile:
    return VmxFile.load(resolve_vmx(vm, config))


def require_offline(vmx_path: Path, operation: str, vmrun: VmRun | None = None) -> None:
    """Guard for edits VMware only honours while the VM is powered off."""
    vmrun = vmrun or VmRun()
    if vmrun.is_running(vmx_path):
        raise VmPoweredOnError(
            f"{operation} requires the VM to be powered off, but "
            f"'{vmx_path.stem}' is running. Call power_vm with action='stop' first "
            "(use 'soft' to let the guest shut down cleanly)."
        )
