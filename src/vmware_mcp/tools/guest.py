"""Tools that reach inside the guest OS through VMware Tools."""

from __future__ import annotations

import contextlib
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from ..errors import VmwareMcpError
from ..inventory import ensure_allowed, ensure_host_io_allowed, load_vmx, resolve_vmx
from ..vmrun import VmRun
from .base import DESTRUCTIVE, MUTATING, READ_ONLY, tool_errors

MAX_OUTPUT_CHARS = 20000


def _is_windows_guest(guest_os: str | None) -> bool:
    value = (guest_os or "").lower()
    return value.startswith("win") or "dos" in value


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        title="Run a command in the guest OS",
        annotations=MUTATING,
        tags={"vmware", "guest"},
    )
    @tool_errors
    def guest_run_command(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        command: Annotated[
            str,
            Field(
                description="Shell command line to run inside the guest. Executed by "
                "cmd.exe /c on Windows guests and /bin/sh -c on Unix guests."
            ),
        ],
        capture_output: Annotated[
            bool,
            Field(
                description="Redirect stdout/stderr to a temp file in the guest and copy "
                "it back. Set false for fire-and-forget commands, or for anything that "
                "produces huge output."
            ),
        ] = True,
        run_as_interactive: Annotated[
            bool,
            Field(
                description="Run in the desktop session of the logged-in user, so GUI "
                "programs are visible. Requires that user to be logged in."
            ),
        ] = False,
        guest_user: Annotated[
            str | None,
            Field(description="Guest username. Falls back to VMWARE_MCP_GUEST_USER."),
        ] = None,
        guest_password: Annotated[
            str | None,
            Field(description="Guest password. Falls back to VMWARE_MCP_GUEST_PASSWORD."),
        ] = None,
    ) -> dict:
        """Run a shell command inside a running guest OS and return its output.

        Requires the VM to be powered on with VMware Tools running, plus guest
        credentials. VMware cannot report the command's exit code, so check the
        output text to judge success.
        """
        vmx = load_vmx(vm)
        vmx_path = vmx.path
        vmrun = VmRun()
        if not vmrun.is_running(vmx_path):
            raise VmwareMcpError(
                f"'{vmx_path.stem}' is powered off. Start it with power_vm before running "
                "guest commands."
            )

        windows = _is_windows_guest(vmx.get("guestOS"))
        creds = {"guest_user": guest_user, "guest_password": guest_password}

        if not capture_output:
            if windows:
                vmrun(
                    "runProgramInGuest",
                    str(vmx_path),
                    *(["-interactive"] if run_as_interactive else []),
                    r"C:\Windows\System32\cmd.exe",
                    "/c",
                    command,
                    **creds,
                )
            else:
                vmrun(
                    "runScriptInGuest",
                    str(vmx_path),
                    *(["-interactive"] if run_as_interactive else []),
                    "/bin/sh",
                    command,
                    **creds,
                )
            return {"vm": vmx_path.stem, "command": command, "output_captured": False}

        token = uuid.uuid4().hex[:12]
        if windows:
            guest_tmp = f"C:\\Windows\\Temp\\vmware-mcp-{token}.txt"
            vmrun(
                "runProgramInGuest",
                str(vmx_path),
                *(["-interactive"] if run_as_interactive else []),
                r"C:\Windows\System32\cmd.exe",
                "/c",
                f'({command}) > "{guest_tmp}" 2>&1',
                **creds,
            )
        else:
            guest_tmp = f"/tmp/vmware-mcp-{token}.txt"
            vmrun(
                "runScriptInGuest",
                str(vmx_path),
                *(["-interactive"] if run_as_interactive else []),
                "/bin/sh",
                f"{{ {command} ; }} > {shlex.quote(guest_tmp)} 2>&1",
                **creds,
            )

        host_tmp = Path(tempfile.gettempdir()) / f"vmware-mcp-{token}.txt"
        try:
            vmrun("CopyFileFromGuestToHost", str(vmx_path), guest_tmp, str(host_tmp), **creds)
            output = host_tmp.read_text(encoding="utf-8", errors="replace")
        finally:
            host_tmp.unlink(missing_ok=True)
            # Cleanup must never mask the command's real result.
            with contextlib.suppress(Exception):
                vmrun("deleteFileInGuest", str(vmx_path), guest_tmp, **creds)

        truncated = len(output) > MAX_OUTPUT_CHARS
        return {
            "vm": vmx_path.stem,
            "command": command,
            "output": output[:MAX_OUTPUT_CHARS],
            "truncated": truncated,
            "note": (
                "Output was truncated; redirect to a file in the guest and copy it back "
                "with guest_copy_file if you need all of it."
            )
            if truncated
            else None,
        }

    @mcp.tool(
        title="Copy a file to or from the guest",
        annotations=MUTATING,
        tags={"vmware", "guest"},
    )
    @tool_errors
    def guest_copy_file(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        direction: Annotated[
            Literal["to_guest", "from_guest"],
            Field(description="Copy host -> guest, or guest -> host."),
        ],
        host_path: Annotated[
            str, Field(description="Absolute path on the host: the source, or the destination.")
        ],
        guest_path: Annotated[
            str, Field(description="Absolute path inside the guest: the destination, or the source.")
        ],
        guest_user: Annotated[
            str | None, Field(description="Guest username. Falls back to VMWARE_MCP_GUEST_USER.")
        ] = None,
        guest_password: Annotated[
            str | None,
            Field(description="Guest password. Falls back to VMWARE_MCP_GUEST_PASSWORD."),
        ] = None,
    ) -> dict:
        """Copy a single file between the host and a running guest OS.

        `host_path` must sit inside a directory named by VMWARE_MCP_HOST_IO_DIRS,
        which is empty by default -- the guest controls what gets written, so the
        operator has to nominate an exchange folder first. Requires VMware Tools
        and guest credentials. Directories are not supported -- archive them
        first. An existing destination is overwritten.
        """
        vmx_path = resolve_vmx(vm)
        # Gate first: the check is unconditional, and reporting it up front stops
        # the caller from powering a VM on only to be refused afterwards.
        ensure_host_io_allowed(Path(host_path), f"guest_copy_file ({direction})")

        vmrun = VmRun()
        if not vmrun.is_running(vmx_path):
            raise VmwareMcpError(f"'{vmx_path.stem}' is powered off. Start it first.")

        creds = {"guest_user": guest_user, "guest_password": guest_password}
        if direction == "to_guest":
            if not Path(host_path).is_file():
                raise VmwareMcpError(f"No file at {host_path} on the host.")
            vmrun("CopyFileFromHostToGuest", str(vmx_path), host_path, guest_path, **creds)
        else:
            Path(host_path).parent.mkdir(parents=True, exist_ok=True)
            vmrun("CopyFileFromGuestToHost", str(vmx_path), guest_path, host_path, **creds)

        return {
            "vm": vmx_path.stem,
            "direction": direction,
            "host_path": host_path,
            "guest_path": guest_path,
            "copied": True,
        }

    @mcp.tool(
        title="List guest processes",
        annotations=READ_ONLY,
        tags={"vmware", "guest", "read"},
    )
    @tool_errors
    def guest_list_processes(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        filter_text: Annotated[
            str | None,
            Field(description="Case-insensitive substring to match against the process rows."),
        ] = None,
        guest_user: Annotated[str | None, Field(description="Guest username.")] = None,
        guest_password: Annotated[str | None, Field(description="Guest password.")] = None,
    ) -> dict:
        """List processes running inside a guest OS, with their PIDs.

        Requires VMware Tools and guest credentials. PIDs returned here are what
        guest_kill_process expects.
        """
        vmx_path = resolve_vmx(vm)
        raw = VmRun()(
            "listProcessesInGuest",
            str(vmx_path),
            guest_user=guest_user,
            guest_password=guest_password,
        )
        lines = [line for line in raw.splitlines() if line.strip()]
        if filter_text:
            needle = filter_text.lower()
            lines = [line for line in lines if needle in line.lower()]
        truncated = len(lines) > 200
        return {
            "vm": vmx_path.stem,
            "count": len(lines),
            "processes": lines[:200],
            "truncated": truncated,
        }

    @mcp.tool(
        title="Kill a guest process",
        annotations=DESTRUCTIVE,
        tags={"vmware", "guest"},
    )
    @tool_errors
    def guest_kill_process(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        pid: Annotated[int, Field(description="Process ID from guest_list_processes.", ge=1)],
        guest_user: Annotated[str | None, Field(description="Guest username.")] = None,
        guest_password: Annotated[str | None, Field(description="Guest password.")] = None,
    ) -> dict:
        """Terminate a process inside a guest OS by PID.

        The process is killed without a chance to save; unsaved data is lost.
        """
        vmx_path = resolve_vmx(vm)
        VmRun()(
            "killProcessInGuest",
            str(vmx_path),
            str(pid),
            guest_user=guest_user,
            guest_password=guest_password,
        )
        return {"vm": vmx_path.stem, "pid": pid, "killed": True}

    @mcp.tool(
        title="Configure a shared folder",
        annotations=MUTATING,
        tags={"vmware", "guest", "config"},
    )
    @tool_errors
    def set_shared_folder(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        action: Annotated[
            Literal["add", "remove", "set_writable", "set_readonly"],
            Field(description="What to do with the share."),
        ],
        share_name: Annotated[
            str, Field(description="Name the guest sees for the share.", min_length=1)
        ],
        host_path: Annotated[
            str | None,
            Field(description="Host folder to share. Required for action='add'."),
        ] = None,
    ) -> dict:
        """Add, remove, or change the access mode of a host-guest shared folder.

        `host_path` must sit inside a directory named by VMWARE_MCP_HOST_IO_DIRS,
        which is empty by default -- a share hands the guest ongoing access to
        that folder, so the operator has to nominate it first. Shared folders also
        need VMware Tools in the guest and the feature enabled -- see
        set_vm_options(shared_folders_enabled=True). On Linux guests the share
        appears under /mnt/hgfs/<share_name>.
        """
        vmx_path = resolve_vmx(vm)
        vmrun = VmRun()

        if host_path is not None:
            ensure_host_io_allowed(Path(host_path), f"set_shared_folder ({action})")

        if action == "add":
            if not host_path:
                raise VmwareMcpError("action='add' needs a host_path to share.")
            if not Path(host_path).is_dir():
                raise VmwareMcpError(f"{host_path} is not a folder on the host.")
            vmrun("addSharedFolder", str(vmx_path), share_name, host_path)
        elif action == "remove":
            vmrun("removeSharedFolder", str(vmx_path), share_name)
        else:
            if not host_path:
                raise VmwareMcpError(
                    f"action='{action}' needs the share's host_path (VMware re-states it "
                    "when changing the mode)."
                )
            mode = "writable" if action == "set_writable" else "readonly"
            vmrun("setSharedFolderState", str(vmx_path), share_name, host_path, mode)

        return {
            "vm": vmx_path.stem,
            "action": action,
            "share_name": share_name,
            "host_path": host_path,
        }

    @mcp.tool(
        title="Capture VM screen",
        # Not READ_ONLY: it reads the VM but writes a file on the host, and the
        # annotation describes side effects on the system, not on the VM.
        annotations=MUTATING,
        tags={"vmware", "guest"},
    )
    @tool_errors
    def capture_screen(
        vm: Annotated[str, Field(description="VM display name, folder, or .vmx path.")],
        output_path: Annotated[
            str | None,
            Field(
                description="Where to write the PNG. Must end in .png, must not already "
                "exist, and must sit inside a configured VM directory. Omit this to get a "
                "uniquely named file in the system temp folder, which is unrestricted."
            ),
        ] = None,
    ) -> dict:
        """Capture the VM's console screen to a PNG on the host.

        Requires the VM to be powered on. Useful for checking an installer or a
        boot screen without opening the Workstation window. Returns the file
        path -- read it with a file tool to view the image.
        """
        vmx_path = resolve_vmx(vm)

        if output_path:
            target = Path(output_path)
            if target.suffix.lower() != ".png":
                raise VmwareMcpError(
                    f"output_path must end in .png; got '{target.name}'. vmrun writes a PNG "
                    "regardless of the name, and restricting the suffix stops this tool "
                    "from being used to clobber other files."
                )
            if target.exists():
                raise VmwareMcpError(
                    f"{target} already exists. This tool never overwrites; choose another "
                    "name or delete the existing file first."
                )
            ensure_allowed(target)
        else:
            # Server-chosen name in temp: no caller-controlled path, nothing to gate.
            target = Path(tempfile.gettempdir()) / f"{vmx_path.stem}-{uuid.uuid4().hex[:8]}.png"

        vmrun = VmRun()
        if not vmrun.is_running(vmx_path):
            raise VmwareMcpError(f"'{vmx_path.stem}' is powered off; there is no screen.")

        target.parent.mkdir(parents=True, exist_ok=True)
        vmrun("captureScreen", str(vmx_path), str(target))
        return {
            "vm": vmx_path.stem,
            "screenshot_path": str(target),
            "size_bytes": target.stat().st_size if target.is_file() else 0,
        }
