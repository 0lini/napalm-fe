"""Unit tests for the CLI parsers."""

import os

import pytest

from napalm_extreme_fe import parsers


@pytest.mark.parametrize(
    "ports, expected",
    [
        ("1/1", ["1/1"]),
        ("1/1-1/3", ["1/1", "1/2", "1/3"]),
        ("1/1,1/3-1/4", ["1/1", "1/3", "1/4"]),
        ("1/49/1-1/49/3", ["1/49/1", "1/49/2", "1/49/3"]),
        ("", []),
        ("-", []),
    ],
)
def test_expand_port_list(ports, expected):
    assert parsers.expand_port_list(ports) == expected


@pytest.mark.parametrize(
    "uptime, expected",
    [
        ("12 day(s), 03:14:15", 1048455.0),
        ("0 day(s), 00:00:05", 5.0),
        ("garbage", -1.0),
    ],
)
def test_parse_uptime(uptime, expected):
    assert parsers.parse_uptime(uptime) == expected


@pytest.mark.parametrize(
    "descr, expected",
    [
        ("5520-24T (9.0.0.0_B020) BoxType: 5520-24T", "9.0.0.0_B020"),
        ("VSP-8284XSQ (8.10.1.0)", "8.10.1.0"),
        ("Fabric Engine 9.1.0.0", "9.1.0.0"),
    ],
)
def test_parse_os_version(descr, expected):
    assert parsers.parse_os_version(descr) == expected


def test_netmask_to_prefixlen():
    assert parsers.netmask_to_prefixlen("255.255.255.0") == 24
    assert parsers.netmask_to_prefixlen("255.255.255.255") == 32


def test_lldp_capabilities():
    # Fabric Engine: R = Repeater, r = Router
    assert parsers._parse_lldp_capabilities("Br / r   (Supported/Enabled)") == (
        ["bridge", "router"],
        ["router"],
    )
    assert parsers._parse_lldp_capabilities("BT / BT") == (
        ["bridge", "telephone"],
        ["bridge", "telephone"],
    )


def test_iter_tables_multiple():
    output = """
=====
  Table A
=====
COL1 COL2
-----
a 1
b 2

All 2 out of 2 displayed
=====
  Table B
=====
COL1 COL2
-----
c 3
"""
    tables = list(parsers.iter_tables(output))
    assert [t[0] for t in tables] == ["Table A", "Table B"]
    assert tables[0][2] == ["a 1", "b 2"]
    assert tables[1][2] == ["c 3"]


SAMPLES = os.path.join(os.path.dirname(__file__), "cli_samples")


def _sample(case, name):
    with open(os.path.join(SAMPLES, case, name)) as handle:
        return handle.read()


def test_empty_real_outputs():
    """Real Fabric Engine 9.4 captures without entries parse to empty results."""
    case = "5320-48P-8XE_9.4.2.0"
    assert parsers.parse_arp_table(_sample(case, "show_ip_arp.txt")) == []
    assert parsers.parse_ip_interfaces(_sample(case, "show_ip_interface_vrfids_0-512.txt")) == {}
    assert parsers.parse_ipv6_interfaces(_sample(case, "show_ipv6_address_interface.txt")) == {}


def test_header_columns():
    assert parsers.header_columns(
        [
            "PORT     IN                  OUT                  IN",
            "NUM      OCTETS               OCTETS               PACKET",
        ]
    ) == ["PORT_NUM", "IN_OCTETS", "OUT_OCTETS", "IN_PACKET"]


def test_iter_tables_dash_before_header_and_subtables():
    output = _sample("5320-48P-8XE_9.4.2.0", "show_interfaces_gigabitEthernet.txt")
    titles = [t[0] for t in parsers.iter_tables(output)]
    assert titles.count("Port Config") == 2  # table + untitled sub-table
    autosense = parsers.tables_by_title(output, "Port Auto-sense")[0]
    assert autosense[2][0].split()[:3] == ["1/1", "Enable", "VOICE"]


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
