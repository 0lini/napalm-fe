"""Tests for the configuration management workflow."""

import pytest
from napalm.base.exceptions import MergeConfigException

from .conftest import PatchedFEDriver


@pytest.fixture
def driver():
    driver = PatchedFEDriver("host", "user", "pass")
    # compare_config() reads the running config of the real Fabric Engine 9.4 capture
    driver.api.current_test = "test_get_config"
    driver.api.current_test_case = "fabric_engine_9.4"
    return driver


CONFIG = """
# comment
interface gigabitEthernet 1/2
name "new port"
exit
"""


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


# Real output of a rejected config line (Fabric Engine 9.4 VM)
COMMUNITY_EXISTS = """\
snmp-server community "napalm-ro" index first secname readgrp

Error: The community entry already exists.
"""


def test_driver_name():
    from napalm import get_network_driver

    assert get_network_driver("fe").__name__ == "FEDriver"


def test_merge_compare_commit(driver):
    driver.load_merge_candidate(config=CONFIG)
    # interface exists already (case-insensitive match), only the name changes
    assert driver.compare_config() == 'interface gigabitEthernet 1/2\n+name "new port"\nexit'
    driver.commit_config()
    assert driver.api.config == [
        "configure terminal",
        "interface gigabitEthernet 1/2",
        'name "new port"',
        "exit",
        "end",
    ]
    assert driver.api.saved == [None]  # "save config" through the API


def test_commit_no_save(driver):
    driver.save_on_commit = False
    driver.load_merge_candidate(config="ntp server 10.0.0.1")
    driver.commit_config()
    assert driver.api.saved == []


def test_get_config_without_saved_config(driver):
    """Real output of a switch that never saved its configuration (9.4 VM)."""
    original = driver.api.cli

    def cli(commands, **kwargs):
        if commands[0].startswith("more "):
            return ['Can\'t open "/intflash/config.cfg" 0x2 to read']
        return original(commands, **kwargs)

    driver.api.cli = cli
    config = driver.get_config()
    assert config["startup"] == ""
    assert config["running"]


def test_config_error_pattern_matches_real_errors():
    from napalm_fe.fe import CLI_ERROR_RE

    assert CLI_ERROR_RE.search(COMMUNITY_EXISTS)
    assert CLI_ERROR_RE.search("% Invalid input detected at '^' marker.")
    # log lines with severity ERROR are not config errors
    log = (
        "1 2026-11-17T15:51:40.608Z 5520-24T-FabricEngine CP1 - 0x00030606 - 00000000 "
        "GlobalRouter SW ERROR Could not open primary config file /intflash/config.cfg"
    )
    assert not CLI_ERROR_RE.search(log)


def test_options():
    driver = PatchedFEDriver("h", "u", "p")
    assert (driver.port, driver.ssl_verify) == (9443, False)
    driver = PatchedFEDriver(
        "h", "u", "p", optional_args={"port": "8443", "ssl_verify": "/etc/ca.pem"}
    )
    assert (driver.port, driver.ssl_verify) == (8443, "/etc/ca.pem")


def test_failed_merge_message(driver):
    """Says that nothing was rolled back (like napalm-dellos10)."""
    driver.api.cli = lambda commands, **kw: ["", COMMUNITY_EXISTS, ""]
    driver.load_merge_candidate(config="foo")
    with pytest.raises(MergeConfigException) as exc:
        driver.commit_config()
    message = str(exc.value)
    assert "automatic rollback not available" in message
    assert "Error: The community entry already exists." in message


# Real reply of the API's CLI operation (Fabric Engine 9.4) for a rejected command
API_INVALID_INPUT = "        ^\n% Invalid input detected at '^' marker.\n"


@pytest.mark.parametrize(
    "outputs",
    [
        ["", API_INVALID_INPUT, "", ""],  # error output (continue_on_failure)
        ["", API_INVALID_INPUT],  # the switch skipped the rest
        ["", "something with Error"],  # skipped after a line the switch counts as failed
    ],
)
def test_merge_error(driver, outputs):
    driver.api.cli = lambda commands, continue_on_failure=True, timeout=None: outputs
    driver.load_merge_candidate(config="foo\nbar")
    with pytest.raises(MergeConfigException, match="automatic rollback not available") as exc:
        driver.commit_config()
    assert "foo" in str(exc.value)
    assert driver.api.saved == []  # not saved


def test_save_failure(driver):
    from napalm.base.exceptions import CommandErrorException

    def save_config(name=None):
        if name is None:
            raise CommandErrorException("OpenAPI POST ... failed: HTTP 500")

    driver.api.save_config = save_config
    driver.load_merge_candidate(config="foo")
    with pytest.raises(CommandErrorException, match="Saving the configuration failed"):
        driver.commit_config()


def test_merge_stops_at_vlan_error(driver):
    """Real error output of the API (9.4) starts with a BEL; "end" is skipped."""
    error = "\n\x07Error: The specified VLAN ID is invalid or already in use \n\n"
    driver.api.cli = lambda commands, **kw: ["Enter configuration commands...\n", error]
    driver.load_merge_candidate(config="vlan create 10 type port-mstprstp 0")
    with pytest.raises(MergeConfigException, match="already in use"):
        driver.commit_config()


def test_commit_confirm_not_supported(driver):
    driver.load_merge_candidate(config=CONFIG)
    with pytest.raises(NotImplementedError):
        driver.commit_config(revert_in=300)
