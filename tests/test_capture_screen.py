"""Regression tests for audit finding 3 — capture_screen as an arbitrary host write."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

GUEST_PY = Path(__file__).resolve().parents[1] / "src" / "vmware_mcp" / "tools" / "guest.py"


def _tool_decorator(name: str) -> ast.Call:
    tree = ast.parse(GUEST_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and getattr(deco.func, "attr", None) == "tool":
                    return deco
    raise AssertionError(f"{name} or its @mcp.tool decorator was not found")


def _body(name: str) -> ast.FunctionDef:
    tree = ast.parse(GUEST_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_capture_screen_is_not_annotated_read_only():
    """READ_ONLY invites hosts to auto-approve; this tool writes to the host."""
    annotations = next(
        kw.value for kw in _tool_decorator("capture_screen").keywords if kw.arg == "annotations"
    )
    assert isinstance(annotations, ast.Name), "expected one of the shared presets"
    assert annotations.id == "MUTATING"


def test_capture_screen_gates_its_output_path():
    calls = {
        node.func.id
        for node in ast.walk(_body("capture_screen"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "ensure_allowed" in calls


@pytest.mark.parametrize("guard", [".png", "already exists"])
def test_capture_screen_rejects_bad_targets(guard):
    """Suffix and overwrite guards are what remove the clobbering primitive."""
    source = ast.get_source_segment(GUEST_PY.read_text(encoding="utf-8"), _body("capture_screen"))
    assert guard in source


def test_read_only_preset_still_means_no_writes():
    """If the preset itself changes, the reasoning above stops holding."""
    from vmware_mcp.tools.base import READ_ONLY

    assert READ_ONLY["readOnlyHint"] is True
