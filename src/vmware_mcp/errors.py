"""Error types surfaced to the MCP client."""

from __future__ import annotations


class VmwareMcpError(Exception):
    """Base error. The message is shown to the model, so it must be actionable.

    Every message should say what went wrong *and* what to try next.
    """


class VmwareNotFoundError(VmwareMcpError):
    """VMware Workstation binaries could not be located on this host."""


class VmNotFoundError(VmwareMcpError):
    """No VM matched the supplied identifier."""


class AmbiguousVmError(VmwareMcpError):
    """More than one VM matched the supplied identifier."""


class PathNotAllowedError(VmwareMcpError):
    """A path falls outside the configured VM directories."""


class HostIoNotAllowedError(VmwareMcpError):
    """A host path for guest file I/O falls outside the configured host-I/O roots."""


class InvalidVmxValueError(VmwareMcpError):
    """A key or value cannot be represented in the .vmx format without corrupting it."""


class DuplicateVmxKeyError(VmwareMcpError):
    """A .vmx defines the same key twice; VMware refuses to open such a file."""


class VmPoweredOnError(VmwareMcpError):
    """An offline-only operation was attempted on a running VM."""


class ToolExecutionError(VmwareMcpError):
    """A VMware command-line tool exited with a failure."""


class DestructiveOpDisabledError(VmwareMcpError):
    """A destructive operation was attempted while the safety gate is closed."""
