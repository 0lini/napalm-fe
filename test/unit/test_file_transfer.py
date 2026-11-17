"""Tests for the Fabric Engine Netmiko file transfer class."""

import os

import pytest

from napalm_extreme_fe.file_transfer import ExtremeFEFileTransfer, parse_ls

SAMPLE = os.path.join(
    os.path.dirname(__file__), "cli_samples", "5320-48P-8XE_9.4.2.0", "ls__intflash.txt"
)


def test_parse_ls():
    with open(SAMPLE) as handle:
        entries = parse_ls(handle.read())
    assert "." not in entries and ".." not in entries
    assert entries["config.cfg"] == {"size": 14385, "is_dir": False}
    assert entries["archive"] == {"size": 224, "is_dir": True}
    assert entries[".cert"]["is_dir"] is True
    assert entries["snmp_comm_moc.txt"] == {"size": 81, "is_dir": False}


class FakeConn:
    device_type = "extreme_vsp"

    def __init__(self, listing):
        self.listing = listing
        self.commands = []

    def _send_command_str(self, command, **kwargs):
        self.commands.append(command)
        return self.listing


@pytest.fixture
def local_file(tmp_path):
    path = tmp_path / "candidate.cfg"
    path.write_text("x" * 14385)
    return str(path)


def test_file_exists_and_size(local_file):
    with open(SAMPLE) as handle:
        conn = FakeConn(handle.read())
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="config.cfg")
    assert transfer.file_system == "/intflash"
    assert transfer.check_file_exists()
    assert transfer.remote_file_size() == 14385
    assert transfer.verify_file()  # same size as the local file
    assert conn.commands[0] == "ls /intflash/config.cfg"
    assert transfer.verify_space_available()


def test_file_missing(local_file):
    conn = FakeConn("Listing Directory /intflash/missing.cfg:\nls: no such file or directory")
    transfer = ExtremeFEFileTransfer(conn, source_file=local_file, dest_file="missing.cfg")
    assert not transfer.check_file_exists()
    assert not transfer.verify_file()
    with pytest.raises(IOError):
        transfer.remote_file_size()


def test_md5_not_supported(local_file):
    transfer = ExtremeFEFileTransfer(FakeConn(""), source_file=local_file, dest_file="a.cfg")
    with pytest.raises(NotImplementedError):
        transfer.remote_md5()
