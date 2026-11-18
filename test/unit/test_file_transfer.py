"""Tests for the Fabric Engine Netmiko file transfer class."""

import hashlib

import pytest

from napalm_extreme_fe.file_transfer import ExtremeFEFileTransfer, parse_dir, parse_dir_free

# Real output of "dir /intflash -l" (Fabric Engine 9.4 VM, trimmed)
DIR_INTFLASH = """\

       -------- INTFLASH --------
  size          date       time       name
--------       ------     ------    --------
    4096    Nov-17-2026  16:41   /intflash/var            <DIR>
    6229    Nov-17-2026  16:43   /intflash/log_of.txt.1
    9852    Nov-17-2026  16:28   /intflash/test1.cfg
       0    Nov-18-2026  09:09   /intflash/reboot_reason.txt
   16384    Apr-09-2026  14:02   /intflash/lost+found     <DIR>
    4096    Nov-17-2026  15:46   /intflash/config         <DIR>

      33    Nov-18-2026  09:07   /intflash/.platform_info
     397    Nov-17-2026  16:28   /intflash/snmp_comm_moc.txt
    4096    Nov-17-2026  15:46   /intflash/.cert          <DIR>
    9852    Nov-17-2026  16:28   /intflash/test2.cfg
total: 6260543488       used: 202092544         free: 6058450944        bytes
"""

MD5_OUTPUT = "MD5 (/intflash/config.cfg) = 5a922729bc0fa85437e7e3b8c339ea34\n"

LOCAL_MD5 = hashlib.md5(b"x" * 9852).hexdigest()


class FakeConn:
    device_type = "extreme_vsp"

    def __init__(self, dir_output=DIR_INTFLASH, md5_output=""):
        self.dir_output = dir_output
        self.md5_output = md5_output
        self.commands = []

    def _send_command_str(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("file-checksum md5"):
            return self.md5_output
        if command.startswith("dir "):
            return self.dir_output
        raise AssertionError(f"unexpected command {command!r}")


@pytest.fixture
def local_file(tmp_path):
    """Local file with the same size as test1.cfg in DIR_INTFLASH."""
    path = tmp_path / "candidate.cfg"
    path.write_text("x" * 9852)
    return str(path)


def test_parse_dir():
    entries = parse_dir(DIR_INTFLASH)
    assert entries["/intflash/test1.cfg"] == {"size": 9852, "is_dir": False}
    assert entries["/intflash/reboot_reason.txt"] == {"size": 0, "is_dir": False}
    assert entries["/intflash/var"] == {"size": 4096, "is_dir": True}
    assert entries["/intflash/lost+found"]["is_dir"] is True
    assert len(entries) == 10
    assert parse_dir_free(DIR_INTFLASH) == 6058450944
    assert parse_dir_free("") is None


def test_process_md5_real_output():
    assert ExtremeFEFileTransfer.process_md5(MD5_OUTPUT) == "5a922729bc0fa85437e7e3b8c339ea34"
    # Fabric Engine 9.4 VM, test1.cfg from DIR_INTFLASH
    vm_output = "MD5 (/intflash/test1.cfg) = 1ea7d1fb127766a9b2b25e86e46ed21d\n"
    assert ExtremeFEFileTransfer.process_md5(vm_output) == "1ea7d1fb127766a9b2b25e86e46ed21d"


def test_file_exists_size_and_md5(local_file):
    conn = FakeConn(md5_output=f"MD5 (/intflash/test1.cfg) = {LOCAL_MD5}")
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="test1.cfg")
    assert transfer.file_system == "/intflash"
    assert transfer.check_file_exists()
    assert transfer.remote_file_size() == 9852
    assert transfer.verify_file()
    assert conn.commands[0] == "dir /intflash -l"
    assert "file-checksum md5 /intflash/test1.cfg" in conn.commands
    assert transfer.verify_space_available()


def test_directory_is_not_a_file(local_file):
    transfer = ExtremeFEFileTransfer(FakeConn(), source_file=local_file, dest_file="config")
    assert not transfer.check_file_exists()


def test_md5_mismatch(local_file):
    conn = FakeConn(md5_output="MD5 (/intflash/test1.cfg) = " + "0" * 32)
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="test1.cfg")
    assert not transfer.verify_file()


def test_size_only_without_hash(local_file):
    conn = FakeConn()
    transfer = ExtremeFEFileTransfer(
        conn, source_file=local_file, dest_file="test1.cfg", hash_supported=False
    )
    assert transfer.verify_file()
    assert not any(c.startswith("file-checksum") for c in conn.commands)


def test_file_missing(local_file):
    transfer = ExtremeFEFileTransfer(FakeConn(), source_file=local_file, dest_file="missing.cfg")
    assert not transfer.check_file_exists()
    assert not transfer.verify_file()
    with pytest.raises(IOError):
        transfer.remote_file_size()


def test_md5_unparsable(local_file):
    transfer = ExtremeFEFileTransfer(
        FakeConn(md5_output="% Error"), source_file=local_file, dest_file="a.cfg"
    )
    with pytest.raises(ValueError):
        transfer.remote_md5()


def test_remote_space_available_real_dir(local_file):
    conn = FakeConn()
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="a.cfg")
    assert transfer.remote_space_available() == 6058450944
    assert transfer.verify_space_available()
    assert conn.commands[-1] == "dir /intflash -l"


def test_insufficient_space(local_file):
    full = "total: 6260543488       used: 6260540000         free: 3488        bytes\n"
    transfer = ExtremeFEFileTransfer(
        FakeConn(dir_output=full), source_file=local_file, dest_file="a.cfg"
    )
    assert not transfer.verify_space_available()  # 9852 bytes do not fit


def test_space_unknown_passes(local_file):
    transfer = ExtremeFEFileTransfer(
        FakeConn(dir_output="% Invalid input"), source_file=local_file, dest_file="a.cfg"
    )
    with pytest.raises(ValueError):
        transfer.remote_space_available()
    assert transfer.verify_space_available()


def test_space_on_usb(local_file):
    conn = FakeConn()
    transfer = ExtremeFEFileTransfer(
        conn, source_file=local_file, dest_file="a.cfg", file_system="/usb"
    )
    assert transfer.verify_space_available()
    assert conn.commands == ["dir /usb -l"]
