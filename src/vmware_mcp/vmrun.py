"""Thin, typed wrapper around ``vmrun.exe`` and ``vmware-vdiskmanager.exe``.

``vmrun`` reports failures by printing ``Error: ...`` on stdout, sometimes while
still exiting 0, so success is decided from the output as well as the exit code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config, get_config
from .errors import ToolExecutionError

# vmrun subcommands that talk to the guest OS and therefore need -gu/-gp.
GUEST_COMMANDS = frozenset(
    {
        "runprogramingguest",
        "runscriptinguest",
        "fileexistsinguest",
        "directoryexistsinguest",
        "listprocessesinguest",
        "killprocessinguest",
        "deletefileinguest",
        "createdirectoryinguest",
        "deletedirectoryinguest",
        "createtempfileinguest",
        "listdirectoryinguest",
        "copyfilefromhosttoguest",
        "copyfilefromguesttohost",
        "renamefileinguest",
        "typekeystrokesinguest",
    }
)

_ERROR_PREFIXES = ("error:", "unable to", "invalid ")


def _decode(data: bytes) -> str:
    for codec in ("utf-8", "cp1252"):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _looks_like_error(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(_ERROR_PREFIXES):
            return stripped
    return None


def _run(
    executable: Path,
    args: list[str],
    *,
    timeout: int,
    label: str,
) -> str:
    if not executable.is_file():
        raise ToolExecutionError(
            f"{executable.name} not found at {executable}. Check that VMware Workstation "
            "is installed, or set VMWARE_MCP_INSTALL_DIR."
        )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, list args, no shell
            [str(executable), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(
            f"{label} timed out after {timeout}s. Raise VMWARE_MCP_TIMEOUT if this "
            "operation is legitimately slow (large clones and disk conversions can be)."
        ) from exc

    stdout = _decode(completed.stdout).strip()
    stderr = _decode(completed.stderr).strip()
    combined = "\n".join(part for part in (stdout, stderr) if part)

    error_line = _looks_like_error(combined)
    if completed.returncode != 0 or error_line:
        detail = error_line or combined or f"exit code {completed.returncode}"
        raise ToolExecutionError(f"{label} failed: {detail}")
    return stdout


class VmRun:
    """Callable facade over ``vmrun.exe``."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()

    def __call__(
        self,
        command: str,
        *args: str,
        guest_user: str | None = None,
        guest_password: str | None = None,
        timeout: int | None = None,
    ) -> str:
        flags = ["-T", "ws"]

        if command.lower() in GUEST_COMMANDS:
            user = guest_user or self.config.guest_user
            password = guest_password if guest_password is not None else self.config.guest_password
            if not user:
                raise ToolExecutionError(
                    f"'{command}' runs inside the guest OS and needs guest credentials. "
                    "Pass guest_user/guest_password to the tool, or set the "
                    "VMWARE_MCP_GUEST_USER and VMWARE_MCP_GUEST_PASSWORD environment "
                    "variables. VMware Tools must also be running in the guest."
                )
            flags += ["-gu", user, "-gp", password or ""]

        return _run(
            self.config.vmrun,
            [*flags, command, *args],
            timeout=timeout or self.config.command_timeout,
            label=f"vmrun {command}",
        )

    # ------------------------------------------------------------- helpers

    def running_vms(self) -> list[Path]:
        """Absolute .vmx paths of every powered-on VM."""
        output = self("list")
        paths: list[Path] = []
        for line in output.splitlines()[1:]:  # first line is "Total running VMs: N"
            line = line.strip()
            if line.lower().endswith(".vmx"):
                paths.append(Path(line))
        return paths

    def is_running(self, vmx_path: Path) -> bool:
        target = str(vmx_path.resolve()).lower()
        return any(str(p).lower() == target for p in self.running_vms())


def vdiskmanager(*args: str, timeout: int | None = None) -> str:
    """Run ``vmware-vdiskmanager.exe`` with the given arguments."""
    config = get_config()
    return _run(
        config.vdiskmanager,
        list(args),
        timeout=timeout or config.command_timeout,
        label="vmware-vdiskmanager",
    )
