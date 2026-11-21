"""Tests for the configuration templates (``load_template``, like NAPALM's drivers).

The commands were checked on Fabric Engine 9.4: ``prompt`` sets the system
name and is what the running configuration shows; ``readgrp`` is the
read-only and ``v1v2grp`` the read-write SNMPv1/v2c group;
``no snmp-server community`` takes the community string.
"""

import pytest

from .conftest import PatchedFEDriver


@pytest.fixture
def driver():
    driver = PatchedFEDriver("host", "user", "pass")
    driver.device.current_test = "test_get_config"
    driver.device.current_test_case = "fabric_engine_9.4"
    return driver


@pytest.mark.parametrize(
    "template, variables, expected",
    [
        ("set_hostname", {"hostname": "sw1"}, 'prompt "sw1"'),
        (
            "set_ntp_servers",
            {"servers": ["192.0.2.1", "192.0.2.2"]},
            "ntp server 192.0.2.1\nntp server 192.0.2.2",
        ),
        ("delete_ntp_servers", {"servers": ["192.0.2.1"]}, "no ntp server 192.0.2.1"),
        (
            "snmp_config",
            {
                "location": "DC1",
                "contact": "noc",
                "chassis_id": "ignored",  # Fabric Engine has no chassis-id setting
                "community": {"ro-comm": {"mode": "ro"}, "rw-comm": {"mode": "rw"}, "x": {}},
            },
            'snmp-server location "DC1"\n'
            'snmp-server contact "noc"\n'
            "snmp-server community ro-comm group readgrp index ro-comm secname ro-comm\n"
            "snmp-server community rw-comm group v1v2grp index rw-comm secname rw-comm\n"
            "snmp-server community x group readgrp index x secname x",
        ),
        (
            "delete_snmp_config",
            {"location": "DC1", "contact": "noc", "community": {"ro-comm": {}}},
            "no snmp-server location\nno snmp-server contact\nno snmp-server community ro-comm",
        ),
    ],
)
def test_template(driver, template, variables, expected):
    driver.load_template(template, **variables)
    assert driver.candidate_config == expected


def test_template_commit_updates_the_prompt(driver):
    prompts = []
    driver.device.set_base_prompt = lambda: prompts.append(1)
    driver.load_template("set_hostname", hostname="sw1")
    assert driver.compare_config() == '+prompt "sw1"'
    driver.commit_config()
    assert driver.device.sent_config == ['prompt "sw1"'] and prompts == [1]
