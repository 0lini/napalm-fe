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
    driver = PatchedFEDriver("host", "user", "pass", optional_args={"transport": "ssh"})
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


# set_users / delete_users: pending user changes, applied by answering the
# prompts over SSH or through configuration/accounts over the API

DEFAULT_USERS = {
    name: {"level": level, "password": "", "sshkeys": []}
    for name, level in (("ro", 1), ("l1", 2), ("l2", 3), ("l3", 4), ("rw", 14), ("rwa", 15))
}


def _users_driver(monkeypatch, transport="ssh", existing=None):
    from napalm_fe import fe

    driver = PatchedFEDriver("host", "user", "pass", optional_args={"transport": transport})
    monkeypatch.setattr(driver, "get_users", lambda: {**DEFAULT_USERS, **(existing or {})})
    assert fe.USER_TEMPLATES == ("set_users", "delete_users")
    return driver


def test_set_users_over_ssh(monkeypatch):
    driver = _users_driver(monkeypatch)
    driver.load_template(
        "set_users",
        users={
            "alice": {"level": 15, "password": "Xq7-Lab-Pass-2026"},
            "bob": {"level": 1, "password": "Zz9-Bob-Pass-2026"},
        },
    )
    diff = driver.compare_config()
    assert diff == "+username add alice level rwa enable\n+username add bob level ro enable"
    assert "Pass" not in diff  # no passwords in the diff
    driver.commit_config()
    assert driver.device.users == {"alice": "rwa", "bob": "ro"}
    assert driver.device.commands == [
        "username add alice level rwa enable",
        "<password>",
        "<password>",
        "username add bob level ro enable",
        "<password>",
        "<password>",
    ]
    assert driver.user_changes == [] and driver.compare_config() == ""


def test_delete_users_over_ssh(monkeypatch):
    driver = _users_driver(monkeypatch, existing={"alice": {"level": 15}})
    driver.device.users = {"alice": "rwa"}
    driver.load_template("delete_users", users={"alice": {}, "unknown": {}})
    assert driver.compare_config() == "-username alice"  # unknown users are skipped
    driver.commit_config()
    assert driver.device.users == {} and driver.device.commands == ["no username alice", "y"]


def test_users_over_https(monkeypatch):
    driver = _users_driver(monkeypatch, transport="https", existing={"old": {"level": 1}})
    driver.load_template(
        "set_users", users={"alice": {"level": 14, "password": "Xq7-Lab-Pass-2026"}}
    )
    driver.commit_config()
    driver.load_template("delete_users", users={"old": {}})
    driver.commit_config()
    assert driver.api.requests == [
        "POST /v0/configuration/accounts {'username': 'alice', 'level': 'ADMIN_LIMITED', "
        "'enabled': True, 'secret': {'isEncrypted': False, 'value': 'Xq7-Lab-Pass-2026'}}",
        "DELETE /v0/configuration/accounts/old",
    ]
    assert not any("cli" in r for r in driver.api.requests)  # never the CLI operation


@pytest.mark.parametrize(
    "template, users, error",
    [
        ("set_users", {"a": {"level": 3, "password": "x"}}, "level must be 1"),
        ("set_users", {"a": {"level": 1}}, "password is required"),
        ("set_users", {"a": {"level": 1, "password": "x", "sshkeys": ["k"]}}, "SSH keys"),
        ("set_users", {"rw": {"level": 14, "password": "x"}}, "already exists"),
        ("delete_users", {"rwa": {}}, "default account"),
    ],
)
def test_user_changes_rejected(monkeypatch, template, users, error):
    from napalm.base.exceptions import MergeConfigException

    driver = _users_driver(monkeypatch)
    with pytest.raises(MergeConfigException, match=error):
        driver.load_template(template, users=users)


def test_set_users_existing_unchanged_and_discard(monkeypatch):
    driver = _users_driver(monkeypatch)
    driver.load_template("set_users", users={"rw": {"level": 14}})  # already so: nothing to do
    assert driver.compare_config() == ""
    driver.load_template(
        "set_users", users={"alice": {"level": 1, "password": "Xq7-Lab-Pass-2026"}}
    )
    driver.discard_config()
    assert driver.compare_config() == "" and driver.user_changes == []


def test_set_users_errors_over_ssh(monkeypatch):
    from napalm.base.exceptions import MergeConfigException

    driver = _users_driver(monkeypatch)
    driver.load_template(
        "set_users", users={"alice": {"level": 1, "password": "Xq7-Lab-Pass-2026"}}
    )
    driver.device.users = {"alice": "ro"}  # created meanwhile: no password prompt
    with pytest.raises(MergeConfigException, match="Failed to create user"):
        driver.commit_config()
    assert "<password>" not in driver.device.commands  # the password is never sent as a command


def test_user_changes_no_revert(monkeypatch):
    driver = _users_driver(monkeypatch)
    driver.load_template("delete_users", users={})
    driver.user_changes = [("delete", "x")]
    with pytest.raises(NotImplementedError):
        driver.commit_config(revert_in=300)
