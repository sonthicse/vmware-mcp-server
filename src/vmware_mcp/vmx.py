"""Order-preserving reader/writer for VMware ``.vmx`` configuration files.

A ``.vmx`` is a flat ``key = "value"`` store. Keys are case-insensitive in
VMware's own parser, so lookups here are too, while the on-disk casing and line
order are preserved -- rewriting a file should produce a minimal diff.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import DuplicateVmxKeyError, InvalidVmxValueError

_LINE_RE = re.compile(r'^(?P<indent>\s*)(?P<key>[^\s=#][^=]*?)\s*=\s*(?P<value>.*?)\s*$')
_ENCODING_RE = re.compile(rb'^\s*\.encoding\s*=\s*"?([\w.-]+)"?', re.IGNORECASE | re.MULTILINE)

# VMware writes these names; anything else we emit is normalised to UTF-8.
_ENCODING_ALIASES = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "windows-1252": "cp1252",
    "cp1252": "cp1252",
    "iso-8859-1": "latin-1",
    "latin-1": "latin-1",
}


MAX_BACKUPS = 5

# The .vmx format has no escape sequence: a value is everything between the
# quotes, terminated by the end of the line. So a quote or a line break inside a
# value cannot be represented -- it either truncates the value or turns the rest
# of the string into additional settings. Rejecting is the only correct handling.
_FORBIDDEN = ((chr(34), "a double quote"), ("\r", "a carriage return"), ("\n", "a line break"))
# A key additionally cannot contain '=' (it would split into key and value) and
# cannot start with '#' (the parser would read the line back as a comment).
_FORBIDDEN_IN_KEY = (*_FORBIDDEN, ("=", "an equals sign"))


def _reject_unrepresentable(kind: str, name: str, text: str) -> None:
    for char, label in _FORBIDDEN_IN_KEY if kind == "key" else _FORBIDDEN:
        if char in text:
            raise InvalidVmxValueError(
                f"Refusing to write {label} into the .vmx {kind} for '{name}'. The .vmx "
                "format has no escape sequence, so this would corrupt the file or silently "
                "append extra settings. Remove the character and retry."
            )
    if kind == "key" and (not text.strip() or text.lstrip().startswith("#")):
        raise InvalidVmxValueError(
            f"'{name}' is not a usable .vmx key: keys cannot be blank or start with '#'."
        )


def backup_files(vmx_path: Path) -> list[Path]:
    """Timestamped backups of *vmx_path*, oldest first."""
    return sorted(vmx_path.parent.glob(f"{vmx_path.stem}.vmx.*.bak"))


def prune_backups(vmx_path: Path, keep: int = MAX_BACKUPS) -> list[Path]:
    """Delete all but the *keep* most recent backups. Returns what was removed."""
    existing = backup_files(vmx_path)
    removed = []
    for stale in existing[: max(0, len(existing) - keep)]:
        with contextlib.suppress(OSError):
            stale.unlink()
            removed.append(stale)
    return removed


def _decode(raw: bytes) -> tuple[str, str]:
    """Return (text, python_codec) honouring the file's own ``.encoding`` key."""
    match = _ENCODING_RE.search(raw)
    declared = match.group(1).decode("ascii", "replace").lower() if match else "utf-8"
    codec = _ENCODING_ALIASES.get(declared, "utf-8")
    try:
        return raw.decode(codec), codec
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace"), "cp1252"


@dataclass
class _Entry:
    key: str
    value: str
    raw: str | None = None  # verbatim line for comments/blanks; None for real entries


class VmxFile:
    """Load, inspect, and rewrite a ``.vmx`` file without reordering it."""

    def __init__(self, path: Path, entries: list[_Entry], codec: str, newline: str) -> None:
        self.path = path
        self._entries = entries
        self._codec = codec
        self._newline = newline

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: Path) -> VmxFile:
        raw = path.read_bytes()
        text, codec = _decode(raw)
        newline = "\r\n" if b"\r\n" in raw else "\n"

        entries: list[_Entry] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                entries.append(_Entry(key="", value="", raw=line))
                continue
            match = _LINE_RE.match(line)
            if not match:
                entries.append(_Entry(key="", value="", raw=line))
                continue
            value = match.group("value")
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            entries.append(_Entry(key=match.group("key").strip(), value=value))
        return cls(path=path, entries=entries, codec=codec, newline=newline)

    # ------------------------------------------------------------- accessors

    def get(self, key: str, default: str | None = None) -> str | None:
        """Value for *key*, taking the last occurrence if the file has duplicates.

        A duplicated key means the file is already invalid -- VMware Workstation
        17.6 refuses to open such a .vmx at all, verified on the host -- so there
        is no "correct" winner to reproduce. Scanning backwards simply matches
        as_dict() and set(), so every accessor on this class agrees. Duplicates
        are surfaced through duplicate_keys() rather than silently resolved.
        """
        target = key.lower()
        for entry in reversed(self._entries):
            if entry.raw is None and entry.key.lower() == target:
                return entry.value
        return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key)
        if value is None:
            return default
        return value.strip().lower() in {"true", "1", "yes"}

    def get_int(self, key: str, default: int | None = None) -> int | None:
        value = self.get(key)
        if value is None:
            return default
        try:
            return int(value.strip())
        except ValueError:
            return default

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def as_dict(self) -> dict[str, str]:
        """All entries. Later duplicates win, consistently with get() and set()."""
        return {e.key: e.value for e in self._entries if e.raw is None}

    def duplicate_keys(self) -> dict[str, list[str]]:
        """Keys appearing more than once, mapped to every value in file order.

        Any result here means the .vmx is corrupt: VMware refuses to open a file
        with a repeated key. Exposed so tools can report it instead of quietly
        picking a winner.
        """
        seen: dict[str, list[str]] = {}
        for entry in self._entries:
            if entry.raw is None:
                seen.setdefault(entry.key.lower(), []).append(entry.value)
        return {k: v for k, v in seen.items() if len(v) > 1}

    def keys_with_prefix(self, prefix: str) -> dict[str, str]:
        lowered = prefix.lower()
        return {
            e.key: e.value
            for e in self._entries
            if e.raw is None and e.key.lower().startswith(lowered)
        }

    # -------------------------------------------------------------- mutation

    def set(self, key: str, value: str | int | bool) -> None:
        """Set a key. Values that cannot be represented in .vmx are rejected here.

        Validating in the serializer rather than per tool means any tool added
        later is protected without having to remember to sanitise its inputs.
        """
        if isinstance(value, bool):
            text = "TRUE" if value else "FALSE"
        else:
            text = str(value)

        _reject_unrepresentable("key", key, key)
        _reject_unrepresentable("value", key, text)

        target = key.lower()
        matches = [e for e in self._entries if e.raw is None and e.key.lower() == target]
        if not matches:
            self._entries.append(_Entry(key=key, value=text))
            return

        # Write to the last occurrence and drop any earlier ones, so touching a
        # key repairs it: a .vmx with a repeated key cannot be opened by VMware.
        matches[-1].value = text
        if len(matches) > 1:
            stale = set(map(id, matches[:-1]))
            self._entries = [e for e in self._entries if id(e) not in stale]

    def update(self, values: dict[str, str | int | bool]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def unset(self, key: str) -> bool:
        target = key.lower()
        before = len(self._entries)
        self._entries = [
            e for e in self._entries if e.raw is not None or e.key.lower() != target
        ]
        return len(self._entries) != before

    def unset_prefix(self, prefix: str) -> list[str]:
        lowered = prefix.lower()
        removed = [
            e.key
            for e in self._entries
            if e.raw is None and e.key.lower().startswith(lowered)
        ]
        if removed:
            self._entries = [
                e
                for e in self._entries
                if e.raw is not None or not e.key.lower().startswith(lowered)
            ]
        return removed

    # ---------------------------------------------------------------- saving

    def render(self) -> str:
        lines = []
        for entry in self._entries:
            if entry.raw is not None:
                lines.append(entry.raw)
            else:
                lines.append(f'{entry.key} = "{entry.value}"')
        return self._newline.join(lines) + self._newline

    def save(self, backup: bool = True, keep_backups: int = MAX_BACKUPS) -> Path | None:
        """Write the file back. Returns the backup path, if one was made.

        Refuses to persist a file with duplicate keys. VMware Workstation 17.6
        will not open such a .vmx at all, so writing one out would leave the VM
        unusable; better to stop and say which key is wrong.
        """
        duplicates = self.duplicate_keys()
        if duplicates:
            listed = "\n".join(
                f"  - {key}: {', '.join(repr(v) for v in values)}"
                for key, values in sorted(duplicates.items())
            )
            raise DuplicateVmxKeyError(
                f"{self.path.name} defines the same key more than once:\n{listed}\n"
                "VMware refuses to open a .vmx with a repeated key, so this file is "
                "already unusable. Remove the wrong copies with unset_vm_config (it "
                "deletes every occurrence), then set the value you want."
            )

        backup_path: Path | None = None
        if backup and self.path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup_path = self.path.with_suffix(f".vmx.{stamp}.bak")
            shutil.copy2(self.path, backup_path)
            prune_backups(self.path, keep=keep_backups)

        # Write to a sibling temp file then replace, so a crash mid-write cannot
        # leave a half-written .vmx that VMware would refuse to open.
        tmp = self.path.with_suffix(".vmx.tmp")
        tmp.write_text(self.render(), encoding=self._codec, newline="")
        tmp.replace(self.path)
        return backup_path


def new_vmx(path: Path, entries: dict[str, str | int | bool]) -> VmxFile:
    """Build a fresh, unsaved VmxFile from an ordered mapping."""
    vmx = VmxFile(path=path, entries=[], codec="utf-8", newline="\n")
    vmx.update(entries)
    return vmx
