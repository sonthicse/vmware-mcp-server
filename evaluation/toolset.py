"""Static view of the server's tool surface, read without importing FastMCP.

The evaluation needs to know which tools exist and what their docstrings
promise, and it needs that answer on a machine with no VMware installed. AST
parsing gives it, the same way tests/test_host_io_gate.py checks call sites.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "vmware_mcp" / "tools"

# A bare tool name mentioned in prose, e.g. "use get_vm_info instead".
_IDENTIFIER_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# Words that look like tool names but are not: env vars, keys, and the
# parameters tools describe to each other.
_NOT_A_TOOL = {
    "file_name",
    "guest_password",
    "guest_user",
    "host_path",
    "guest_path",
    "iso_path",
    "output_path",
    "new_size_gb",
    "size_gb",
    "disk_size_gb",
    "existing_vmdk",
    "cores_per_socket",
    "memory_mb",
    "graphics_memory_mb",
    "num_displays",
    "hardware_version",
    "connection_type",
    "virtual_device",
    "mac_address",
    "connect_at_power_on",
    "guest_os",
    "display_name",
    "new_name",
    "clone_type",
    "disk_type",
    "controller_type",
    "disk_controller",
    "network_type",
    "independent_mode",
    "secure_boot",
    "sync_time_with_host",
    "shared_folders_enabled",
    "power_off_type",
    "enable_drag_and_drop",
    "enable_clipboard",
    "virtualize_vtx",
    "virtualize_iommu",
    "accelerate_3d",
    "capture_output",
    "run_as_interactive",
    "filter_text",
    "share_name",
    "delete_file",
    "delete_children",
    "remove_drive",
    "include_disk_details",
    "running_only",
    "show_tree",
    "confirm_name",
    "host_port",
    "guest_port",
    "guest_ip",
    "vm_dirs",
    "auto_detect",
    "cdrom_image",
    "cdrom_raw",
}


@dataclass(frozen=True)
class Tool:
    name: str
    module: str
    title: str
    annotations: str
    docstring: str

    def referenced_tools(self, known: set[str]) -> set[str]:
        """Tool names this tool's docstring points the model at."""
        found = set(_IDENTIFIER_RE.findall(self.docstring)) - _NOT_A_TOOL
        return {name for name in found if name != self.name and name in known}

    def dangling_references(self, known: set[str]) -> set[str]:
        """Names that read like a tool but match nothing registered.

        A stale cross-reference is worse than no cross-reference: the model
        trusts the prose and calls a tool that does not exist.
        """
        candidates = set(_IDENTIFIER_RE.findall(self.docstring)) - _NOT_A_TOOL
        verbs = ("get_", "list_", "set_", "add_", "remove_", "create_", "delete_",
                 "power_", "attach_", "detach_", "resize_", "optimize_", "clone_",
                 "revert_", "capture_", "guest_", "unset_", "manage_", "check_")
        return {
            name
            for name in candidates
            if name not in known
            and name != self.name
            and name.startswith(verbs)
        }


def _decorator_arg(decorator: ast.expr, keyword: str) -> str:
    if not isinstance(decorator, ast.Call):
        return ""
    for kw in decorator.keywords:
        if kw.arg != keyword:
            continue
        if isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
        if isinstance(kw.value, ast.Name):
            return kw.value.id
    return ""


def _is_tool_decorator(decorator: ast.expr) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "tool"
    )


def registered_tools(tools_dir: Path = TOOLS_DIR) -> dict[str, Tool]:
    """Every function decorated with @mcp.tool, keyed by function name."""
    tools: dict[str, Tool] = {}
    for source in sorted(tools_dir.glob("*.py")):
        if source.name in {"__init__.py", "base.py"}:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorator = next(
                (d for d in node.decorator_list if _is_tool_decorator(d)), None
            )
            if decorator is None:
                continue
            tools[node.name] = Tool(
                name=node.name,
                module=source.stem,
                title=_decorator_arg(decorator, "title"),
                annotations=_decorator_arg(decorator, "annotations"),
                docstring=ast.get_docstring(node) or "",
            )
    return tools
