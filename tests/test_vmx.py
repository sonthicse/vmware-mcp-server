from __future__ import annotations

import pytest

from vmware_mcp.errors import DuplicateVmxKeyError, InvalidVmxValueError
from vmware_mcp.vmx import VmxFile, new_vmx

SAMPLE = (
    '.encoding = "UTF-8"\n'
    'config.version = "8"\n'
    "# a comment\n"
    "\n"
    'displayName = "Test VM"\n'
    'numvcpus = "2"\n'
    'memsize = "2048"\n'
    'ethernet0.present = "TRUE"\n'
    'ethernet0.connectionType = "nat"\n'
)


@pytest.fixture
def vmx(tmp_path):
    path = tmp_path / "Test VM.vmx"
    path.write_text(SAMPLE, encoding="utf-8", newline="")
    return VmxFile.load(path)


def test_reads_values(vmx):
    assert vmx.get("displayName") == "Test VM"
    assert vmx.get_int("memsize") == 2048
    assert vmx.get_bool("ethernet0.present") is True
    assert vmx.get("missing.key", "fallback") == "fallback"


def test_lookup_is_case_insensitive(vmx):
    assert vmx.get("DISPLAYNAME") == "Test VM"
    assert vmx.get("Ethernet0.ConnectionType") == "nat"


def test_set_updates_in_place_and_preserves_order(vmx):
    vmx.set("MEMSIZE", 4096)
    rendered = vmx.render()
    assert rendered.count("memsize") == 1
    assert 'memsize = "4096"' in rendered
    # original casing survives the case-insensitive write
    assert "MEMSIZE" not in rendered
    assert rendered.index("displayName") < rendered.index("memsize")


def test_set_appends_new_keys(vmx):
    vmx.set("vhv.enable", True)
    assert vmx.get("vhv.enable") == "TRUE"
    assert vmx.render().rstrip().endswith('vhv.enable = "TRUE"')


def test_booleans_render_as_vmware_literals(vmx):
    vmx.set("a.flag", False)
    assert vmx.get("a.flag") == "FALSE"


def test_comments_and_blank_lines_survive(vmx):
    rendered = vmx.render()
    assert "# a comment" in rendered
    assert "\n\n" in rendered


def test_unset_and_unset_prefix(vmx):
    assert vmx.unset("numvcpus") is True
    assert vmx.unset("numvcpus") is False
    removed = vmx.unset_prefix("ethernet0.")
    assert sorted(removed) == ["ethernet0.connectionType", "ethernet0.present"]
    assert "ethernet0" not in vmx.render()


def test_keys_with_prefix(vmx):
    assert vmx.keys_with_prefix("ETHERNET0") == {
        "ethernet0.present": "TRUE",
        "ethernet0.connectionType": "nat",
    }


def test_save_roundtrips_and_backs_up(vmx):
    vmx.set("memsize", 8192)
    backup = vmx.save()

    assert backup is not None and backup.exists()
    assert 'memsize = "2048"' in backup.read_text(encoding="utf-8")
    assert VmxFile.load(vmx.path).get_int("memsize") == 8192
    assert not vmx.path.with_suffix(".vmx.tmp").exists()


def test_save_honours_declared_cp1252_encoding(tmp_path):
    path = tmp_path / "legacy.vmx"
    path.write_bytes('.encoding = "windows-1252"\nannotation = "caf\xe9"\n'.encode("cp1252"))

    vmx = VmxFile.load(path)
    assert vmx.get("annotation") == "café"

    vmx.set("displayName", "légacy")
    vmx.save(backup=False)
    assert path.read_bytes().decode("cp1252").count("légacy") == 1


def test_crlf_line_endings_are_preserved(tmp_path):
    path = tmp_path / "crlf.vmx"
    path.write_bytes(b'.encoding = "UTF-8"\r\nmemsize = "1024"\r\n')

    vmx = VmxFile.load(path)
    vmx.set("memsize", 2048)
    vmx.save(backup=False)
    assert b"\r\n" in path.read_bytes()


class TestUnrepresentableValues:
    """Regression tests for audit finding 2 — .vmx has no escape sequence, so a
    line break or quote in a value would append attacker-chosen settings."""

    def test_line_break_in_value_is_rejected(self, vmx):
        payload = 'notes"\nsharedFolder0.hostPath = "C:\\'
        with pytest.raises(InvalidVmxValueError):
            vmx.set("annotation", payload)
        # Nothing was written, so the file is untouched.
        assert not vmx.has("sharedFolder0.hostPath")

    def test_carriage_return_in_value_is_rejected(self, vmx):
        with pytest.raises(InvalidVmxValueError):
            vmx.set("annotation", 'a\rvhv.enable = "TRUE"')

    def test_quote_in_value_is_rejected(self, vmx):
        with pytest.raises(InvalidVmxValueError):
            vmx.set("annotation", 'say "hello"')

    def test_injection_via_key_is_rejected(self, vmx):
        with pytest.raises(InvalidVmxValueError):
            vmx.set('annotation" = "x\nvhv.enable', "TRUE")
        with pytest.raises(InvalidVmxValueError):
            vmx.set("a = b", "x")
        with pytest.raises(InvalidVmxValueError):
            vmx.set("# comment", "x")
        with pytest.raises(InvalidVmxValueError):
            vmx.set("   ", "x")

    def test_error_message_names_the_key(self, vmx):
        with pytest.raises(InvalidVmxValueError, match="annotation"):
            vmx.set("annotation", "line\nbreak")

    def test_ordinary_values_still_pass(self, vmx):
        vmx.set("annotation", "ubuntu:ubuntu — notes with dashes, commas & 'quotes'")
        vmx.set("numvcpus", 8)
        vmx.set("vhv.enable", True)
        vmx.set("sata0:1.fileName", r"C:\iso\ubuntu-26.04.iso")
        assert vmx.get_int("numvcpus") == 8
        assert vmx.get("vhv.enable") == "TRUE"

    def test_new_vmx_is_protected_too(self, tmp_path):
        with pytest.raises(InvalidVmxValueError):
            new_vmx(tmp_path / "x.vmx", {"guestOS": 'ubuntu-64"\nvhv.enable = "TRUE'})


class TestDuplicateKeys:
    """Regression tests for audit finding 4.

    The audit assumed VMware resolves duplicates last-wins. Probing Workstation
    17.6 on the host showed it instead refuses to open such a .vmx at all
    (a control file without the duplicate opened fine), so a repeat is plain
    corruption and the class must never produce or persist one.
    """

    @pytest.fixture
    def dup(self, tmp_path):
        path = tmp_path / "dup.vmx"
        path.write_text(
            '.encoding = "UTF-8"\n'
            'vhv.enable = "TRUE"\n'
            'memsize = "1024"\n'
            'vhv.enable = "FALSE"\n',
            encoding="utf-8",
            newline="",
        )
        return VmxFile.load(path)

    def test_accessors_no_longer_disagree(self, dup):
        assert dup.get("vhv.enable") == dup.as_dict()["vhv.enable"]
        assert dup.get("vhv.enable") == "FALSE"
        assert dup.get_bool("vhv.enable") is False

    def test_duplicates_are_reported(self, dup):
        assert dup.duplicate_keys() == {"vhv.enable": ["TRUE", "FALSE"]}
        assert "memsize" not in dup.duplicate_keys()

    def test_set_collapses_duplicates_of_that_key(self, dup):
        dup.set("vhv.enable", True)
        assert dup.duplicate_keys() == {}
        assert dup.get("vhv.enable") == "TRUE"
        assert dup.render().count("vhv.enable") == 1

    def test_set_keeps_the_last_position(self, dup):
        dup.set("vhv.enable", True)
        rendered = dup.render()
        assert rendered.index("memsize") < rendered.index("vhv.enable")

    def test_save_refuses_a_file_that_vmware_cannot_open(self, dup):
        with pytest.raises(DuplicateVmxKeyError, match="vhv.enable"):
            dup.save(backup=False)

    def test_save_succeeds_once_repaired(self, dup):
        dup.set("vhv.enable", True)
        dup.save(backup=False)
        assert VmxFile.load(dup.path).duplicate_keys() == {}

    def test_unset_then_set_is_a_repair_path(self, dup):
        dup.unset("vhv.enable")
        dup.set("vhv.enable", False)
        dup.save(backup=False)
        assert VmxFile.load(dup.path).get("vhv.enable") == "FALSE"

    def test_clean_files_are_unaffected(self, vmx):
        assert vmx.duplicate_keys() == {}
        vmx.set("memsize", 4096)
        vmx.save(backup=False)
        assert VmxFile.load(vmx.path).get_int("memsize") == 4096


def test_backups_are_pruned_to_the_most_recent(vmx, monkeypatch):
    import itertools

    stamps = itertools.count()
    monkeypatch.setattr(
        "vmware_mcp.vmx.time.strftime", lambda _fmt: f"20260101-0000{next(stamps):02d}"
    )
    for size in range(1024, 1024 + 8 * 4, 4):
        vmx.set("memsize", size)
        vmx.save(keep_backups=3)

    backups = sorted(vmx.path.parent.glob("*.bak"))
    assert len(backups) == 3
    # The survivors are the newest three, by timestamp.
    assert [b.name.split(".")[-2] for b in backups] == [
        "20260101-000005",
        "20260101-000006",
        "20260101-000007",
    ]


def test_new_vmx_builds_from_mapping(tmp_path):
    vmx = new_vmx(tmp_path / "fresh.vmx", {"displayName": "Fresh", "numvcpus": 4})
    assert vmx.get("displayName") == "Fresh"
    assert vmx.get("numvcpus") == "4"
