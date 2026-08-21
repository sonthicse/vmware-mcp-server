"""Interpreting the device half of a ``.vmx``: controllers, disks, NICs, CD-ROMs."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from .vmx import VmxFile

# controller prefix -> (max unit index, reserved units)
CONTROLLERS: dict[str, tuple[int, set[int]]] = {
    "scsi": (15, {7}),  # unit 7 is the controller itself
    "sata": (29, set()),
    "nvme": (14, set()),
    "ide": (1, set()),
}
MAX_CONTROLLERS = {"scsi": 4, "sata": 4, "nvme": 4, "ide": 2}

SCSI_ADAPTERS = ("lsilogic", "lsisas1068", "pvscsi", "buslogic")

CDROM_TYPES = {"cdrom-image", "cdrom-raw", "atapi-cdrom"}
DISK_TYPES = {"disk", "rawdisk", "scsi-hardDisk", "ata-hardDisk"}

_NODE_RE = re.compile(r"^(?P<controller>(?:scsi|sata|nvme|ide)\d+):(?P<unit>\d+)$", re.IGNORECASE)
_EXTENT_RE = re.compile(r'^(?:RW|RDONLY|NOACCESS)\s+(\d+)\s+\w+', re.MULTILINE)


def _node_keys(vmx: VmxFile) -> dict[str, dict[str, str]]:
    """Group ``scsi0:0.fileName`` style keys by their ``controller:unit`` node."""
    nodes: dict[str, dict[str, str]] = {}
    for key, value in vmx.as_dict().items():
        head, _, prop = key.partition(".")
        if not prop:
            continue
        match = _NODE_RE.match(head)
        if not match:
            continue
        nodes.setdefault(head.lower(), {})[prop.lower()] = value
    return nodes


def classify_node(props: dict[str, str]) -> str:
    device_type = props.get("devicetype", "").lower()
    if device_type in {t.lower() for t in CDROM_TYPES}:
        return "cdrom"
    if device_type in {t.lower() for t in DISK_TYPES}:
        return "disk"
    if device_type:
        return device_type
    file_name = props.get("filename", "")
    if file_name.lower().endswith(".vmdk"):
        return "disk"
    if file_name.lower().endswith(".iso"):
        return "cdrom"
    return "unknown"


def list_controllers(vmx: VmxFile) -> list[dict]:
    out = []
    for family, limit in MAX_CONTROLLERS.items():
        for index in range(limit):
            name = f"{family}{index}"
            if not vmx.get_bool(f"{name}.present"):
                continue
            out.append(
                {
                    "name": name,
                    "family": family,
                    "virtual_device": vmx.get(f"{name}.virtualDev"),
                }
            )
    return out


def vmdk_info(vmdk_path: Path) -> dict:
    """Best-effort capacity/size for a ``.vmdk``. Missing values come back None."""
    info: dict = {
        "path": str(vmdk_path),
        "exists": vmdk_path.is_file(),
        "capacity_gb": None,
        "size_on_disk_gb": None,
    }
    if not vmdk_path.is_file():
        return info

    # A split disk stores data in -s001.vmdk siblings; a preallocated one in -flat.
    total = 0
    for sibling in vmdk_path.parent.glob(f"{vmdk_path.stem}*.vmdk"):
        with contextlib.suppress(OSError):
            total += sibling.stat().st_size
    info["size_on_disk_gb"] = round(total / 1024**3, 3)

    try:
        header = vmdk_path.open("rb").read(65536).decode("latin-1")
    except OSError:
        return info
    sectors = sum(int(m) for m in _EXTENT_RE.findall(header))
    if sectors:
        info["capacity_gb"] = round(sectors * 512 / 1024**3, 3)
    return info


def list_disks(vmx: VmxFile, include_details: bool = True) -> list[dict]:
    base = vmx.path.parent
    disks = []
    for node, props in sorted(_node_keys(vmx).items()):
        if classify_node(props) != "disk":
            continue
        if props.get("present", "TRUE").upper() == "FALSE":
            continue
        file_name = props.get("filename", "")
        entry: dict = {
            "node": node,
            "file_name": file_name,
            "mode": props.get("mode"),
            "present": True,
        }
        if include_details and file_name:
            candidate = Path(file_name)
            resolved = candidate if candidate.is_absolute() else base / candidate
            entry.update(vmdk_info(resolved))
        disks.append(entry)
    return disks


def list_cdroms(vmx: VmxFile) -> list[dict]:
    out = []
    for node, props in sorted(_node_keys(vmx).items()):
        if classify_node(props) != "cdrom":
            continue
        out.append(
            {
                "node": node,
                "device_type": props.get("devicetype"),
                "file_name": props.get("filename"),
                "present": props.get("present", "TRUE").upper() == "TRUE",
                "start_connected": props.get("startconnected", "TRUE").upper() == "TRUE",
            }
        )
    return out


def list_nics(vmx: VmxFile) -> list[dict]:
    out = []
    for index in range(10):
        prefix = f"ethernet{index}"
        if not vmx.get_bool(f"{prefix}.present"):
            continue
        connection = vmx.get(f"{prefix}.connectionType", "nat")
        out.append(
            {
                "node": prefix,
                "connection_type": connection,
                "vmnet": vmx.get(f"{prefix}.vnet"),
                "virtual_device": vmx.get(f"{prefix}.virtualDev"),
                "address_type": vmx.get(f"{prefix}.addressType"),
                "mac_address": vmx.get(f"{prefix}.address")
                or vmx.get(f"{prefix}.generatedAddress"),
                "start_connected": vmx.get_bool(f"{prefix}.startConnected", True),
                "display_name": vmx.get(f"{prefix}.displayName"),
            }
        )
    return out


def next_free_node(vmx: VmxFile, family: str) -> str:
    """First unused ``family<N>:<unit>`` slot, creating a controller if needed."""
    family = family.lower()
    if family not in CONTROLLERS:
        raise ValueError(f"Unknown controller family '{family}'.")
    max_unit, reserved = CONTROLLERS[family]
    nodes = _node_keys(vmx)
    controllers = [f"{family}{i}" for i in range(MAX_CONTROLLERS[family])]

    def first_gap(controller: str) -> str | None:
        for unit in range(max_unit + 1):
            if unit in reserved:
                continue
            node = f"{controller}:{unit}"
            if node not in nodes:
                return node
        return None

    # Fill controllers the VM already has before adding another one.
    present = [c for c in controllers if vmx.get_bool(f"{c}.present")]
    for controller in [*present, *(c for c in controllers if c not in present)]:
        node = first_gap(controller)
        if node:
            return node
    raise ValueError(f"No free {family} slots left on this VM.")


def next_free_nic(vmx: VmxFile) -> str:
    for index in range(10):
        if not vmx.has(f"ethernet{index}.present"):
            return f"ethernet{index}"
    raise ValueError("This VM already has the maximum of 10 network adapters.")


def controller_of(node: str) -> str:
    return node.split(":", 1)[0]


def ensure_controller(vmx: VmxFile, node: str) -> None:
    """Make sure the controller backing *node* is declared present."""
    controller = controller_of(node)
    family = re.sub(r"\d+$", "", controller)
    vmx.set(f"{controller}.present", True)
    if family == "scsi" and not vmx.get(f"{controller}.virtualDev"):
        vmx.set(f"{controller}.virtualDev", "lsilogic")
    if family == "nvme" and not vmx.get(f"{controller}.virtualDev"):
        vmx.set(f"{controller}.virtualDev", "nvme")
