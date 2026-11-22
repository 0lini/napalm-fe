"""Tests for the configuration management workflow."""

import re

import pytest
from napalm.base.exceptions import MergeConfigException
from netmiko.exceptions import ConfigInvalidException

from .conftest import PatchedFEDriver


@pytest.fixture
def driver():
    driver = PatchedFEDriver("host", "user", "pass")
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


def test_netmiko_device_type():
    from netmiko.ssh_dispatcher import CLASS_MAPPER

    from napalm_fe.fe import NETMIKO_DEVICE_TYPE

    assert NETMIKO_DEVICE_TYPE in CLASS_MAPPER


def test_save_failure_detected(driver):
    from napalm.base.exceptions import CommandErrorException

    driver.device.save_config = lambda *a, **kw: "save config\nSave config failed."
    driver.load_merge_candidate(config="ntp server 192.0.2.99")
    with pytest.raises(CommandErrorException):
        driver.commit_config()


def test_sanitize_filters():
    from napalm.base.helpers import sanitize_config

    from napalm_fe.fe import SANITIZE_FILTERS

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


# --- rollback file (like the IOS driver: saved before every commit) --------


# --- replace (Site Engine style: install as boot config + reboot) ----------


# --- rollback -----------------------------------------------------------------


# Real output of a rejected config line (Fabric Engine 9.4 VM)
COMMUNITY_EXISTS = """\
snmp-server community "napalm-ro" index first secname readgrp

Error: The community entry already exists.
"""


def test_config_error_pattern_matches_real_errors():

    from napalm_fe.fe import CLI_ERROR_RE

    pattern = CLI_ERROR_RE.pattern  # as passed to Netmiko, which applies re.M
    assert re.search(pattern, COMMUNITY_EXISTS, flags=re.M)
    assert re.search(pattern, "% Invalid input detected at '^' marker.", flags=re.M)
    # log lines with severity ERROR are not config errors
    log = (
        "1 2026-11-17T15:51:40.608Z 5520-24T-FabricEngine CP1 - 0x00030606 - 00000000 "
        "GlobalRouter SW ERROR Could not open primary config file /intflash/config.cfg"
    )
    assert not re.search(pattern, log, flags=re.M)


def test_merge_fails_on_error_without_percent(driver):
    driver.device.send_config_set = lambda lines, **kw: COMMUNITY_EXISTS
    driver.load_merge_candidate(
        config='snmp-server community "napalm-ro" index first secname readgrp'
    )
    with pytest.raises(MergeConfigException, match="already exists"):
        driver.commit_config()


def test_get_config_without_saved_config(driver):
    """Real output of a switch that never saved its configuration (9.4 VM)."""
    original = driver.device.send_command

    def send_command(command, **kwargs):
        if command.startswith("more "):
            return 'Can\'t open "/intflash/config.cfg" 0x2 to read'
        return original(command, **kwargs)

    driver.device.send_command = send_command
    config = driver.get_config()
    assert config["startup"] == ""
    assert config["running"]


def test_driver_name():
    from napalm import get_network_driver

    assert get_network_driver("fe").__name__ == "FEDriver"


def test_failed_merge_message(driver):
    """Says that nothing was rolled back (like napalm-dellos10)."""
    driver.device.send_config_set = lambda lines, **kw: COMMUNITY_EXISTS
    driver.load_merge_candidate(config="foo")
    with pytest.raises(MergeConfigException) as exc:
        driver.commit_config()
    message = str(exc.value)
    assert "automatic rollback not available" in message
    assert "Error: The community entry already exists." in message


def test_commit_updates_the_prompt(driver):
    """A new system name (prompt "...") changes the SSH prompt: Netmiko's base
    prompt is updated after the commit, like the NX-OS driver does."""
    prompts = []
    driver.device.set_base_prompt = lambda: prompts.append(1)
    driver.load_merge_candidate(config='prompt "sw1"')
    driver.commit_config()
    assert driver.device.sent_config == ['prompt "sw1"'] and prompts == [1]


def test_commit_confirm_not_supported(driver):
    driver.load_merge_candidate(config=CONFIG)
    with pytest.raises(NotImplementedError):
        driver.commit_config(revert_in=300)
