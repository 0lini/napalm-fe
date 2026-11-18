"""Tests for the Fabric Engine Netmiko file transfer class."""

import hashlib

import pytest

from napalm_extreme_fe.file_transfer import ExtremeFEFileTransfer, parse_ls

# Real outputs of Fabric Engine 9.4 (directory listing trimmed)
LS_INTFLASH = """\
Listing Directory /intflash:
drwxr-xr-x 35 0       0           5048 Nov 16 23:58  ./
drwxr-xr-x  3 0       0            232 Mar 28  2025  ../
-rw-------  1 0       0            108 Nov 16 12:24  .auto_sense_key.txt
drwxr-xr-x 15 0       0           1256 Mar 28  2025  .cert/
-rw-r--r--  1 0       0          11900 Nov 17 11:28  CpProfiler.txt
drwxr-xr-x  3 0       0            224 Mar 28  2025  archive/
-rwx------  1 0       0          14385 Nov 16 12:24  config.cfg
drwxr-xr-x  2 0       0            160 Mar 28  2025  config/
--w-------  1 0       0             81 Nov 16 12:24  snmp_comm_moc.txt
"""

LS_MISSING = """\
Listing Directory /intflash:
Can't open "/intflash/doesnotexist.cfg".
"""

# last lines of "dir" (Fabric Engine 9.4 VM)
DIR_OUTPUT = """\
./openapi
total: 6260543488       used: 202080256         free: 6058463232        bytes
"""

MD5_OUTPUT = "MD5 (/intflash/config.cfg) = 5a922729bc0fa85437e7e3b8c339ea34\n"

LOCAL_MD5 = hashlib.md5(b"x" * 14385).hexdigest()


class FakeConn:
    device_type = "extreme_vsp"

    def __init__(self, listing, md5_output="", dir_output=DIR_OUTPUT):
        self.listing = listing
        self.md5_output = md5_output
        self.dir_output = dir_output
        self.commands = []

    def _send_command_str(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("file-checksum md5"):
            return self.md5_output
        if command == "dir":
            return self.dir_output
        return self.listing


@pytest.fixture
def local_file(tmp_path):
    """Local file with the same size as config.cfg in LS_INTFLASH."""
    path = tmp_path / "candidate.cfg"
    path.write_text("x" * 14385)
    return str(path)


def test_parse_ls():
    entries = parse_ls(LS_INTFLASH)
    assert "." not in entries and ".." not in entries
    assert entries["config.cfg"] == {"size": 14385, "is_dir": False}
    assert entries["archive"] == {"size": 224, "is_dir": True}
    assert entries[".cert"]["is_dir"] is True
    assert entries["snmp_comm_moc.txt"] == {"size": 81, "is_dir": False}


def test_process_md5_real_output():
    assert ExtremeFEFileTransfer.process_md5(MD5_OUTPUT) == "5a922729bc0fa85437e7e3b8c339ea34"


def test_file_exists_size_and_md5(local_file):
    conn = FakeConn(LS_INTFLASH, md5_output=f"MD5 (/intflash/config.cfg) = {LOCAL_MD5}")
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="config.cfg")
    assert transfer.file_system == "/intflash"
    assert transfer.check_file_exists()
    assert transfer.remote_file_size() == 14385
    assert transfer.verify_file()
    assert conn.commands[0] == "ls /intflash/config.cfg"
    assert "file-checksum md5 /intflash/config.cfg" in conn.commands
    assert transfer.verify_space_available()


def test_md5_mismatch(local_file):
    conn = FakeConn(LS_INTFLASH, md5_output="MD5 (/intflash/config.cfg) = " + "0" * 32)
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="config.cfg")
    assert not transfer.verify_file()


def test_size_only_without_hash(local_file):
    conn = FakeConn(LS_INTFLASH)
    transfer = ExtremeFEFileTransfer(
        conn, source_file=local_file, dest_file="config.cfg", hash_supported=False
    )
    assert transfer.verify_file()
    assert not any(c.startswith("file-checksum") for c in conn.commands)


def test_file_missing(local_file):
    conn = FakeConn(LS_MISSING)
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="doesnotexist.cfg")
    assert not transfer.check_file_exists()
    assert not transfer.verify_file()
    with pytest.raises(IOError):
        transfer.remote_file_size()


def test_md5_unparsable(local_file):
    transfer = ExtremeFEFileTransfer(
        FakeConn("", md5_output="% Error"), source_file=local_file, dest_file="a.cfg"
    )
    with pytest.raises(ValueError):
        transfer.remote_md5()


def test_remote_space_available_real_dir(local_file):
    transfer = ExtremeFEFileTransfer(FakeConn(""), source_file=local_file, dest_file="a.cfg")
    assert transfer.remote_space_available() == 6058463232
    assert transfer.verify_space_available()


def test_insufficient_space(local_file):
    full = "total: 6260543488       used: 6260540000         free: 3488        bytes\n"
    transfer = ExtremeFEFileTransfer(
        FakeConn("", dir_output=full), source_file=local_file, dest_file="a.cfg"
    )
    assert not transfer.verify_space_available()  # 14385 bytes do not fit


def test_space_unknown_passes(local_file):
    transfer = ExtremeFEFileTransfer(
        FakeConn("", dir_output="% Invalid input"), source_file=local_file, dest_file="a.cfg"
    )
    with pytest.raises(ValueError):
        transfer.remote_space_available()
    assert transfer.verify_space_available()


def test_space_not_checked_outside_intflash(local_file):
    conn = FakeConn("")
    transfer = ExtremeFEFileTransfer(
        conn, source_file=local_file, dest_file="a.cfg", file_system="/usb"
    )
    assert transfer.verify_space_available()
    assert "dir" not in conn.commands
