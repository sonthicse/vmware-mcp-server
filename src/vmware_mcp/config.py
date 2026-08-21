"""Runtime configuration: where VMware lives, which directories are writable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .errors import VmwareNotFoundError

DEFAULT_INSTALL_DIRS = (
    r"C:\Program Files (x86)\VMware\VMware Workstation",
    r"C:\Program Files\VMware\VMware Workstation",
    r"C:\Program Files (x86)\VMware\VMware Player",
    r"C:\Program Files\VMware\VMware Player",
)

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _env_paths(name: str) -> list[Path]:
    raw = os.environ.get(name, "")
    out: list[Path] = []
    for chunk in raw.replace(";", os.pathsep).split(os.pathsep):
        chunk = chunk.strip().strip('"')
        if chunk:
            out.append(Path(chunk))
    return out


def _install_dir_from_registry() -> Path | None:
    try:
        import winreg
    except ImportError:  # non-Windows host
        return None

    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VMware, Inc.\VMware Workstation"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VMware, Inc.\VMware Workstation"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VMware, Inc.\VMware Player"),
    )
    for root, subkey in keys:
        try:
            with winreg.OpenKey(root, subkey) as handle:
                value, _ = winreg.QueryValueEx(handle, "InstallPath")
        except OSError:
            continue
        candidate = Path(str(value))
        if candidate.is_dir():
            return candidate
    return None


def _discover_install_dir() -> Path:
    override = os.environ.get("VMWARE_MCP_INSTALL_DIR")
    if override:
        candidate = Path(override)
        if not (candidate / "vmrun.exe").is_file():
            raise VmwareNotFoundError(
                f"VMWARE_MCP_INSTALL_DIR points at {candidate}, but vmrun.exe is not there. "
                "Set it to the folder containing vmrun.exe (e.g. "
                r'"C:\Program Files (x86)\VMware\VMware Workstation").'
            )
        return candidate

    from_registry = _install_dir_from_registry()
    if from_registry and (from_registry / "vmrun.exe").is_file():
        return from_registry

    for raw in DEFAULT_INSTALL_DIRS:
        candidate = Path(raw)
        if (candidate / "vmrun.exe").is_file():
            return candidate

    raise VmwareNotFoundError(
        "Could not locate VMware Workstation. Install it, or set the "
        "VMWARE_MCP_INSTALL_DIR environment variable to the folder containing vmrun.exe."
    )


def _default_vm_dirs() -> list[Path]:
    """Directories VMware itself uses for VMs, in priority order."""
    dirs: list[Path] = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        home = Path(userprofile)
        dirs.append(home / "Documents" / "Virtual Machines")
        dirs.append(home / "OneDrive" / "Documents" / "Virtual Machines")
    onedrive = os.environ.get("ONEDRIVE")
    if onedrive:
        dirs.append(Path(onedrive) / "Documents" / "Virtual Machines")
    public = os.environ.get("PUBLIC")
    if public:
        dirs.append(Path(public) / "Documents" / "Shared Virtual Machines")
    return dirs


@dataclass(frozen=True)
class Config:
    install_dir: Path
    vm_dirs: tuple[Path, ...]
    # Separate, opt-in roots for host<->guest file traffic. Deliberately NOT
    # vm_dirs: letting an untrusted guest write into a VM directory would let it
    # overwrite the .vmx this server reads back, or another VM's disks.
    host_io_dirs: tuple[Path, ...]
    allow_any_path: bool
    allow_destructive: bool
    guest_user: str | None
    guest_password: str | None
    command_timeout: int

    @property
    def vmrun(self) -> Path:
        return self.install_dir / "vmrun.exe"

    @property
    def vdiskmanager(self) -> Path:
        return self.install_dir / "vmware-vdiskmanager.exe"

    @property
    def vmware_exe(self) -> Path:
        return self.install_dir / "vmware.exe"

    @property
    def ovftool(self) -> Path:
        return self.install_dir / "OVFTool" / "ovftool.exe"

    @property
    def inventory_file(self) -> Path:
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "VMware" / "inventory.vmls"

    @property
    def preferences_file(self) -> Path:
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "VMware" / "preferences.ini"

    @property
    def default_vm_dir(self) -> Path:
        """Where newly created VMs go when the caller does not say."""
        for candidate in self.vm_dirs:
            if candidate.is_dir():
                return candidate
        if self.vm_dirs:
            return self.vm_dirs[0]
        return Path(os.environ.get("USERPROFILE", ".")) / "Documents" / "Virtual Machines"


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Build the config once per process. Environment is read only here."""
    extra_dirs = _env_paths("VMWARE_MCP_VM_DIRS")

    seen: set[str] = set()
    vm_dirs: list[Path] = []
    for candidate in [*extra_dirs, *_default_vm_dirs()]:
        key = str(candidate).rstrip("\\/").lower()
        if key and key not in seen:
            seen.add(key)
            vm_dirs.append(candidate)

    timeout_raw = os.environ.get("VMWARE_MCP_TIMEOUT", "300")
    try:
        timeout = max(10, int(timeout_raw))
    except ValueError:
        timeout = 300

    return Config(
        install_dir=_discover_install_dir(),
        vm_dirs=tuple(vm_dirs),
        host_io_dirs=tuple(_env_paths("VMWARE_MCP_HOST_IO_DIRS")),
        allow_any_path=_env_bool("VMWARE_MCP_ALLOW_ANY_PATH"),
        allow_destructive=_env_bool("VMWARE_MCP_ALLOW_DESTRUCTIVE"),
        guest_user=os.environ.get("VMWARE_MCP_GUEST_USER") or None,
        guest_password=os.environ.get("VMWARE_MCP_GUEST_PASSWORD") or None,
        command_timeout=timeout,
    )
