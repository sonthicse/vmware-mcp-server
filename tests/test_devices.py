from __future__ import annotations

import pytest
from conftest import make_vm

from vmware_mcp.devices import (
    ensure_controller,
    list_cdroms,
    list_disks,
    list_nics,
    next_free_nic,
    next_free_node,
)
from vmware_mcp.vmx import VmxFile


@pytest.fixture
def vmx(vm_root):
    return VmxFile.load(make_vm(vm_root, "Alpha"))


def test_lists_the_boot_disk(vmx):
    disks = list_disks(vmx, include_details=False)
    assert [d["node"] for d in disks] == ["scsi0:0"]
    assert disks[0]["file_name"] == "Alpha.vmdk"


def test_cdrom_is_classified_separately(vm_root):
    vmx = VmxFile.load(
        make_vm(
            vm_root,
            "WithIso",
            **{
                "sata0.present": "TRUE",
                "sata0:1.present": "TRUE",
                "sata0:1.deviceType": "cdrom-image",
                "sata0:1.fileName": r"C:\iso\ubuntu.iso",
            },
        )
    )
    assert [d["node"] for d in list_disks(vmx, include_details=False)] == ["scsi0:0"]
    cdroms = list_cdroms(vmx)
    assert [c["node"] for c in cdroms] == ["sata0:1"]
    assert cdroms[0]["file_name"].endswith("ubuntu.iso")


def test_lists_nics(vmx):
    nics = list_nics(vmx)
    assert len(nics) == 1
    assert nics[0]["node"] == "ethernet0"
    assert nics[0]["connection_type"] == "nat"


def test_next_free_node_skips_occupied_and_reserved(vmx):
    # scsi0:0 is taken; unit 7 is the controller and must be skipped.
    assert next_free_node(vmx, "scsi") == "scsi0:1"
    for unit in range(1, 16):
        vmx.set(f"scsi0:{unit}.present", True)
    assert next_free_node(vmx, "scsi") == "scsi1:0"


def test_next_free_node_on_an_absent_controller(vmx):
    assert next_free_node(vmx, "nvme") == "nvme0:0"


def test_next_free_nic(vmx):
    assert next_free_nic(vmx) == "ethernet1"


def test_ensure_controller_creates_defaults(vmx):
    ensure_controller(vmx, "nvme0:0")
    assert vmx.get_bool("nvme0.present") is True
    assert vmx.get("nvme0.virtualDev") == "nvme"


def test_ensure_controller_leaves_existing_alone(vmx):
    vmx.set("scsi0.virtualDev", "pvscsi")
    ensure_controller(vmx, "scsi0:1")
    assert vmx.get("scsi0.virtualDev") == "pvscsi"
