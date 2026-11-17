"""Tests for the configuration management workflow."""

import pytest
from napalm.base.exceptions import MergeConfigException, ReplaceConfigException
from netmiko.exceptions import ConfigInvalidException

from .conftest import PatchedExtremeFEDriver


@pytest.fixture
def driver():
    driver = PatchedExtremeFEDriver("host", "user", "pass")
    # compare_config() reads the running config of the real Fabric Engine 9.4 capture
    driver.device.current_test = "test_get_config"
    driver.device.current_test_case = "fabric_engine_9.4"
    return driver


CONFIG = """
# comment
interface gigabitEthernet 1/2
name "new port"
exit
"""


def test_merge_compare_commit(driver):
    driver.load_merge_candidate(config=CONFIG)
    # interface exists already (case-insensitive match), only the name changes
    assert driver.compare_config() == 'interface gigabitEthernet 1/2\n+name "new port"\nexit'
    driver.commit_config()
    assert driver.device.sent_config == [
        "interface gigabitEthernet 1/2",
        'name "new port"',
        "exit",
    ]
    assert driver.compare_config() == ""


def test_merge_from_file(driver, tmp_path):
    path = tmp_path / "cfg.txt"
    path.write_text("ntp server 10.0.0.1\n")
    driver.load_merge_candidate(filename=str(path))
    assert driver.compare_config() == "+ntp server 10.0.0.1"


def test_compare_incremental(driver):
    driver.load_merge_candidate(
        config="\n".join(
            [
                "ntp server 192.0.2.21",  # already configured -> not shown
                "no ntp server 192.0.2.22",  # configured -> removal
                "no snmp-server location",  # not configured -> addition of the "no" form
                "interface GigabitEthernet 1/1",
                "no shutdown",  # already configured
                "exit",
                "interface vlan 2000",  # new context
                "ip address 10.0.0.1/24",
                "exit",
            ]
        )
    )
    assert driver.compare_config().splitlines() == [
        "-ntp server 192.0.2.22",
        "+no snmp-server location",
        "+interface vlan 2000",
        "+ip address 10.0.0.1/24",
        "exit",
    ]


def test_compare_no_changes(driver):
    driver.load_merge_candidate(config="ntp server 192.0.2.21\nsys mtu 9600")
    assert driver.compare_config() == ""


def test_merge_missing_file(driver):
    with pytest.raises(MergeConfigException):
        driver.load_merge_candidate(filename="/does/not/exist")


def test_discard(driver):
    driver.load_merge_candidate(config="ntp server 10.0.0.1")
    driver.discard_config()
    assert driver.compare_config() == ""


def test_commit_without_candidate(driver):
    with pytest.raises(MergeConfigException):
        driver.commit_config()


def test_commit_error(driver):
    def fail(lines, **kwargs):
        assert kwargs.get("error_pattern")
        raise ConfigInvalidException("foo\n% Invalid input detected at '^' marker.")

    driver.device.send_config_set = fail
    driver.load_merge_candidate(config="foo")
    with pytest.raises(MergeConfigException):
        driver.commit_config()


def test_commit_no_save(driver):
    driver.save_on_commit = False
    driver.device.save_config = lambda *a, **kw: pytest.fail("save_config must not be called")
    driver.load_merge_candidate(config="ntp server 10.0.0.1")
    driver.commit_config()


def test_replace_not_supported(driver):
    with pytest.raises(ReplaceConfigException):
        driver.load_replace_candidate(config="foo")


def test_netmiko_device_type():
    from netmiko.ssh_dispatcher import CLASS_MAPPER

    from napalm_extreme_fe.extreme_fe import NETMIKO_DEVICE_TYPE

    assert NETMIKO_DEVICE_TYPE in CLASS_MAPPER
    assert NETMIKO_DEVICE_TYPE in ("extreme_fe", "extreme_voss", "extreme_vsp")


class FakeTransfer:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        with open(kwargs["source_file"]) as handle:
            self.content = handle.read()
        FakeTransfer.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def transfer_file(self):
        pass

    def verify_file(self):
        return True


@pytest.fixture
def file_driver(monkeypatch):
    from napalm_extreme_fe import extreme_fe

    FakeTransfer.instances = []
    monkeypatch.setattr(extreme_fe, "ExtremeFEFileTransfer", FakeTransfer)
    driver = PatchedExtremeFEDriver("host", "user", "pass", optional_args={"config_method": "file"})
    driver.device.current_test = "test_get_config"
    driver.device.current_test_case = "fabric_engine_9.4"
    sent = []
    original = driver.device.send_command

    def send_command(command, **kwargs):
        sent.append(command)
        if command.startswith("save config file"):
            # real output of Fabric Engine 9.4.2.0
            return (
                f"CP-1: Save config to file {command.split()[-1]} successful.\n"
                'WARNING: Choice Primary Node Config file is "/intflash/config.cfg".'
            )
        if command.startswith("source "):
            return ""
        return original(command, **kwargs)

    driver.device.send_command = send_command
    driver.sent_commands = sent
    return driver


def test_file_method_upload_and_commit(file_driver):
    file_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    upload = FakeTransfer.instances[0]
    assert upload.kwargs["dest_file"] == "napalm_merge.cfg"
    assert upload.kwargs["file_system"] == "/intflash"
    assert upload.content == "config terminal\nntp server 192.0.2.99\nend\n"

    assert file_driver.compare_config() == "+ntp server 192.0.2.99"
    file_driver.commit_config()
    assert file_driver.sent_commands[-2:] == [
        "save config file /intflash/napalm_rollback.cfg",
        "source /intflash/napalm_merge.cfg",
    ]
    assert file_driver.device.sent_config == []  # nothing sent line by line


def test_file_method_source_error(file_driver):
    file_driver.load_merge_candidate(config="bogus command")
    file_driver.device.send_command = lambda command, **kw: (
        "% Invalid input detected at '^' marker."
        if command.startswith("source")
        else "CP-1: Save config to file /intflash/napalm_rollback.cfg successful."
    )
    with pytest.raises(MergeConfigException, match="napalm_rollback.cfg"):
        file_driver.commit_config()


def test_invalid_config_method():
    with pytest.raises(ValueError):
        PatchedExtremeFEDriver("host", "user", "pass", optional_args={"config_method": "x"})


def test_rollback_not_supported(driver):
    with pytest.raises(NotImplementedError):
        driver.rollback()


def test_file_method_restore_point_failure(file_driver):
    file_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    file_driver.device.send_command = lambda command, **kw: "Error: flash is full"
    with pytest.raises(MergeConfigException, match="restore point"):
        file_driver.commit_config()


def test_save_failure_detected(driver):
    from napalm.base.exceptions import CommandErrorException

    driver.device.save_config = lambda *a, **kw: "save config\nSave config failed."
    driver.load_merge_candidate(config="ntp server 192.0.2.99")
    with pytest.raises(CommandErrorException):
        driver.commit_config()


def test_sanitize_filters():
    from napalm.base.helpers import sanitize_config

    from napalm_extreme_fe.extreme_fe import SANITIZE_FILTERS

    config = "\n".join(
        [
            "password hash sha2",
            "auto-sense isis hello-auth type hmac-sha-256",
            "isis hello-auth type hmac-md5 key isiskey key-id 1",
            "radius server host 10.0.0.1 key ******  used-by eapol  priority 1",
            "radius dynamic-server client 10.0.0.1 secret s3cr3t enable",
            'snmp-server community "public" index first secname readgrp',
            "snmp-server user admin md5 authpass aes privpass",
            "ntp authentication-key 1 md5 ntpkey",
            "#",
        ]
    )
    assert sanitize_config(config, SANITIZE_FILTERS).splitlines() == [
        "password hash sha2",
        "auto-sense isis hello-auth type hmac-sha-256",
        "isis hello-auth type hmac-md5 key <removed> key-id 1",
        "radius server host 10.0.0.1 key <removed>  used-by eapol  priority 1",
        "radius dynamic-server client 10.0.0.1 secret <removed> enable",
        "snmp-server community <removed> index first secname readgrp",
        "snmp-server user admin md5 <removed> aes <removed>",
        "ntp authentication-key 1 md5 <removed>",
        "#",
    ]
