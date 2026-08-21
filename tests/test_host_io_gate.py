"""Regression tests for audit finding 1 — host paths crossing the guest boundary."""

from __future__ import annotations

import dataclasses

import pytest

from vmware_mcp.errors import HostIoNotAllowedError
from vmware_mcp.inventory import ensure_host_io_allowed


def with_host_io(config, *dirs):
    return dataclasses.replace(config, host_io_dirs=tuple(dirs))


def test_disabled_by_default(config, tmp_path):
    with pytest.raises(HostIoNotAllowedError) as exc:
        ensure_host_io_allowed(tmp_path / "anything.txt", "guest_copy_file", config)
    # The message must name the variable that turns it on.
    assert "VMWARE_MCP_HOST_IO_DIRS" in str(exc.value)


def test_allows_paths_inside_an_approved_directory(config, exchange_dir):
    cfg = with_host_io(config, exchange_dir)
    target = exchange_dir / "payload.bin"
    assert ensure_host_io_allowed(target, "guest_copy_file", cfg) == target
    nested = exchange_dir / "sub" / "deep.bin"
    assert ensure_host_io_allowed(nested, "guest_copy_file", cfg) == nested


def test_blocks_paths_outside_it(config, exchange_dir, tmp_path):
    cfg = with_host_io(config, exchange_dir)
    with pytest.raises(HostIoNotAllowedError):
        ensure_host_io_allowed(tmp_path / "startup" / "evil.ps1", "guest_copy_file", cfg)


def test_blocks_traversal_out_of_an_approved_directory(config, exchange_dir):
    cfg = with_host_io(config, exchange_dir)
    escape = exchange_dir / ".." / "elsewhere" / "evil.ps1"
    with pytest.raises(HostIoNotAllowedError):
        ensure_host_io_allowed(escape, "guest_copy_file", cfg)


def test_blocks_sibling_directory_sharing_a_prefix(config, tmp_path):
    approved = tmp_path / "Exchange"
    approved.mkdir()
    sibling = tmp_path / "Exchange-evil"
    sibling.mkdir()
    cfg = with_host_io(config, approved)
    with pytest.raises(HostIoNotAllowedError):
        ensure_host_io_allowed(sibling / "x.bin", "guest_copy_file", cfg)


def test_vm_dirs_do_not_satisfy_the_host_io_gate(config, vm_root):
    """The VM tree is explicitly not an exchange folder: a guest writing there
    could overwrite the .vmx this server reads back."""
    with pytest.raises(HostIoNotAllowedError):
        ensure_host_io_allowed(vm_root / "Alpha" / "Alpha.vmx", "guest_copy_file", config)


def test_allow_any_path_does_not_open_the_host_io_gate(config, tmp_path):
    permissive = dataclasses.replace(config, allow_any_path=True)
    with pytest.raises(HostIoNotAllowedError):
        ensure_host_io_allowed(tmp_path / "evil.ps1", "guest_copy_file", permissive)


def test_every_host_path_tool_calls_the_gate():
    """Guard against the gate being dropped again during a refactor.

    The original defect was not a broken check but a missing call, so this
    asserts on the call sites rather than on behaviour.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "vmware_mcp" / "tools" / "guest.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    gated = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call.func, ast.Name) and call.func.id == "ensure_host_io_allowed"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
    }
    assert {"guest_copy_file", "set_shared_folder"} <= gated
