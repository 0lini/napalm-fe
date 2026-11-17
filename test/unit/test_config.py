"""Tests for the configuration management workflow."""

import pytest
from napalm.base.exceptions import MergeConfigException, ReplaceConfigException
from netmiko.exceptions import ConfigInvalidException

from .conftest import PatchedExtremeFEDriver


@pytest.fixture
def driver():
    driver = PatchedExtremeFEDriver("host", "user", "pass")
    # compare_config() reads the running config of the real 5320 capture
    driver.device.current_test = "test_get_config"
    driver.device.current_test_case = "5320-48P-8XE_9.4.2.0"
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
