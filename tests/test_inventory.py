from __future__ import annotations

import pytest
from conftest import make_vm

from vmware_mcp.errors import AmbiguousVmError, PathNotAllowedError, VmNotFoundError
from vmware_mcp.inventory import ensure_allowed, is_allowed, resolve_vmx, scan_vm_dirs


def test_scan_finds_vms(config, vm_root):
    make_vm(vm_root, "Alpha")
    make_vm(vm_root, "Beta")

    found = {ref.name for ref in scan_vm_dirs(config)}
    assert found == {"Alpha", "Beta"}


def test_resolve_by_exact_name(config, vm_root):
    expected = make_vm(vm_root, "Alpha")
    assert resolve_vmx("Alpha", config) == expected.resolve()


def test_resolve_is_case_insensitive(config, vm_root):
    expected = make_vm(vm_root, "Ubuntu Server")
    assert resolve_vmx("ubuntu server", config) == expected.resolve()


def test_resolve_by_unique_substring(config, vm_root):
    expected = make_vm(vm_root, "Ubuntu Server 26.04 LTS")
    make_vm(vm_root, "Windows 11")
    assert resolve_vmx("Ubuntu", config) == expected.resolve()


def test_ambiguous_substring_is_rejected(config, vm_root):
    make_vm(vm_root, "Ubuntu 24.04")
    make_vm(vm_root, "Ubuntu 26.04")

    with pytest.raises(AmbiguousVmError) as exc:
        resolve_vmx("Ubuntu", config)
    # The error must list the candidates so the caller can disambiguate.
    assert "Ubuntu 24.04" in str(exc.value)
    assert "Ubuntu 26.04" in str(exc.value)


def test_resolve_by_folder(config, vm_root):
    expected = make_vm(vm_root, "Alpha")
    assert resolve_vmx(str(vm_root / "Alpha"), config) == expected.resolve()


def test_resolve_by_full_vmx_path(config, vm_root):
    expected = make_vm(vm_root, "Alpha")
    assert resolve_vmx(str(expected), config) == expected.resolve()


def test_unknown_name_lists_known_vms(config, vm_root):
    make_vm(vm_root, "Alpha")
    with pytest.raises(VmNotFoundError) as exc:
        resolve_vmx("Nope", config)
    assert "Alpha" in str(exc.value)


def test_empty_identifier_is_rejected(config):
    with pytest.raises(VmNotFoundError):
        resolve_vmx("   ", config)


def test_path_outside_vm_dirs_is_blocked(config, tmp_path, vm_root):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = make_vm(outside, "Stray")

    assert is_allowed(stray, config) is False
    with pytest.raises(PathNotAllowedError) as exc:
        resolve_vmx(str(stray), config)
    assert "VMWARE_MCP_VM_DIRS" in str(exc.value)


def test_allow_any_path_disables_the_gate(config, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = make_vm(outside, "Stray")

    permissive = config.__class__(**{**config.__dict__, "allow_any_path": True})
    assert ensure_allowed(stray, permissive) == stray
