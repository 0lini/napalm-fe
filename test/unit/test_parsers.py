"""Unit tests for the CLI parsers."""

import os

import pytest

from napalm_fe import parsers


@pytest.mark.parametrize(
    "descr, expected",
    [
        ("5520-24T (9.0.0.0_B020) BoxType: 5520-24T", "9.0.0.0_B020"),
        ("5520-24T-FabricEngine (9.3.0.0)", "9.3.0.0"),  # 9.3 VM
    ],
)
def test_parse_os_version(descr, expected):
    assert parsers.parse_os_version(descr) == expected


def test_netmask_to_prefixlen():
    assert parsers.netmask_to_prefixlen("255.255.255.0") == 24
    assert parsers.netmask_to_prefixlen("255.255.255.255") == 32


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


MOCKED_DATA = os.path.join(os.path.dirname(__file__), "mocked_data")


def _mocked(test, case, name):
    with open(os.path.join(MOCKED_DATA, test, case, name)) as handle:
        return handle.read()


# real output (Fabric Engine 9.4) of a switch without entries
IP_INTERFACE_EMPTY = """\
************************************************************************************
                Command Execution Time: Tue Nov 17 11:12:20 2026 CET
************************************************************************************

========================================================================================================================
                                              IP Interface - GlobalRouter
========================================================================================================================
INTERFACE    IP               NET              ADMIN      OPER     VLAN  BROUTER    IPSEC   ACCESS             IP
             ADDRESS          MASK             STATUS     STATE    ID    PORT       STATE                      NAME
------------------------------------------------------------------------------------------------------------------------


All 0 out of 0 Total Num of IP interfaces displayed


Info: No VRF exists with id: 1-512
"""

# real output (Fabric Engine 9.4) of a switch without entries
IPV6_ADDRESS_EMPTY = """\
************************************************************************************
                Command Execution Time: Tue Nov 17 11:12:33 2026 CET
************************************************************************************
========================================================================================================================
                                           Address Information - GlobalRouter
========================================================================================================================
IPV6 ADDRESS/PREFIX LENGTH                   VID/BID/TID  TYPE    ORIGIN    STATUS    VALID    PREF           IPv6
                                                                                      LIFETIME LIFETIME       NAME
------------------------------------------------------------------------------------------------------------------------

0 out of 0 Total Num of Address Entries displayed.
"""


def test_empty_real_outputs():
    """Real outputs without entries parse to empty results."""
    assert parsers.parse_ip_interfaces(IP_INTERFACE_EMPTY) == {}
    assert parsers.parse_ipv6_interfaces(IPV6_ADDRESS_EMPTY) == {}


def test_header_columns():
    assert parsers.header_columns(
        [
            "PORT     IN                  OUT                  IN",
            "NUM      OCTETS               OCTETS               PACKET",
        ]
    ) == ["PORT_NUM", "IN_OCTETS", "OUT_OCTETS", "IN_PACKET"]


def test_iter_tables_dash_before_header_and_subtables():
    output = _mocked(
        "test_get_interfaces", "fabric_engine_9.4", "show_interfaces_gigabitEthernet.txt"
    )
    titles = [t[0] for t in parsers.iter_tables(output)]
    assert titles.count("Port Config") == 2  # table + untitled sub-table
    autosense = parsers.tables_by_title(output, "Port Auto-sense")[0]
    assert autosense[2][0].split()[:3] == ["1/1", "Enable", "VOICE"]


# Real output (Fabric Engine 9.4 VM): temperature sensors without a description
SYS_INFO_VM_TEMPERATURE = """\
Temperature Info :

                        Sensor                  Warning   Critical
                   Description   Temperature  Threshold  Threshold
                                           0        N/A        N/A
                                           0        N/A        N/A
                                           0        N/A        N/A


Power Supply Info :

        Ps#1 Status       : UP
"""


def test_temperature_without_description():
    result = parsers.parse_sys_info_temperature(SYS_INFO_VM_TEMPERATURE)
    assert list(result) == ["Sensor 1", "Sensor 2", "Sensor 3"]
    assert result["Sensor 1"] == {"temperature": 0.0, "is_alert": False, "is_critical": False}
