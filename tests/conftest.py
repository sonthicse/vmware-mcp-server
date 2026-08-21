from __future__ import annotations

from pathlib import Path

import pytest

from vmware_mcp.config import Config


@pytest.fixture
def vm_root(tmp_path) -> Path:
    root = tmp_path / "Virtual Machines"
    root.mkdir()
    return root


@pytest.fixture
def exchange_dir(tmp_path) -> Path:
    root = tmp_path / "VM-Exchange"
    root.mkdir()
    return root


@pytest.fixture
def config(tmp_path, vm_root) -> Config:
    """A Config pinned to a temp tree, so tests never touch real VMs."""
    install = tmp_path / "VMware"
    install.mkdir()
    (install / "vmrun.exe").write_bytes(b"")
    (install / "vmware-vdiskmanager.exe").write_bytes(b"")

    return Config(
        install_dir=install,
        vm_dirs=(vm_root,),
        host_io_dirs=(),
        allow_any_path=False,
        allow_destructive=False,
        guest_user=None,
        guest_password=None,
        command_timeout=60,
    )


def make_vm(root: Path, name: str, **extra: str) -> Path:
    """Create a minimal VM folder and return its .vmx path."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    vmx = folder / f"{name}.vmx"
    lines = [
        '.encoding = "UTF-8"',
        'config.version = "8"',
        f'displayName = "{name}"',
        'numvcpus = "2"',
        'memsize = "2048"',
        'guestOS = "ubuntu-64"',
        'scsi0.present = "TRUE"',
        'scsi0.virtualDev = "lsilogic"',
        'scsi0:0.present = "TRUE"',
        f'scsi0:0.fileName = "{name}.vmdk"',
        'ethernet0.present = "TRUE"',
        'ethernet0.connectionType = "nat"',
    ]
    lines += [f'{k} = "{v}"' for k, v in extra.items()]
    vmx.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    (folder / f"{name}.vmdk").write_bytes(b"")
    return vmx
