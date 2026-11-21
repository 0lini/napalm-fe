"""Tests for get_users and set_users / delete_users (load_template).

The commands and prompts were checked on Fabric Engine 9.4: ``username add
<name> level <l> enable`` asks the password twice, ``cli password <name>
<level>`` the old one and twice the new one, ``no username <name>`` asks
(y/n).
"""

import pytest

from .conftest import PatchedFEDriver

# set_users / delete_users: pending user changes, applied by answering the
# prompts (like ExtremeCloud IQ Site Engine)

DEFAULT_USERS = {
    name: {"level": level, "password": "", "sshkeys": []}
    for name, level in (("ro", 1), ("l1", 2), ("l2", 3), ("l3", 4), ("rw", 14), ("rwa", 15))
}


def _users_driver(monkeypatch, existing=None):
    from napalm_fe import fe

    driver = PatchedFEDriver("host", "user", "pass")
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


@pytest.mark.parametrize(
    "template, users, error",
    [
        ("set_users", {"a": {"level": 3, "password": "x"}}, "level must be 1"),
        ("set_users", {"a": {"level": 1}}, "password is required"),
        ("set_users", {"a": {"level": 1, "password": "x", "sshkeys": ["k"]}}, "SSH keys"),
        ("set_users", {"rw": {"level": 14, "password": "x"}}, "only with the old one"),
        ("set_users", {"rw": {"level": 15, "password": "x", "old_password": "y"}}, "level"),
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


def test_change_password(monkeypatch):
    """Fabric Engine changes a password only with the old one (9.4: CLI and API)."""
    driver = _users_driver(monkeypatch)
    users = {"rwa": {"level": 15, "password": "New-Pass-2026x", "old_password": "Old-Pass-2025x"}}
    driver.load_template("set_users", users=users)
    assert driver.compare_config() == "+cli password rwa read-write-all (new password)"
    driver.commit_config()
    assert driver.device.password_changes == ["rwa"]
    assert driver.device.commands == ["cli password rwa read-write-all", *["<password>"] * 3]
