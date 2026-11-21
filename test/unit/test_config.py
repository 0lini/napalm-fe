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


# Real log message printed into an SSH session ("logging screen"), Fabric Engine 9.4 VM
LOG_LINE = (
    "1 2026-11-17T16:11:12.756Z 5520-24T-FabricEngine CP1 - 0x000305ca - 00000000 "
    "GlobalRouter SW INFO us"
)
PROMPT = "5520-24T-FabricEngine:1>"


def test_log_messages_are_removed_from_output():
    from napalm_fe.fe import FEDriver

    output = f"VLAN    PORT\n{LOG_LINE}\n1       1/1\n{PROMPT}{LOG_LINE}\n10      1/2\n"
    assert FEDriver._send_command_postprocess(output) == "VLAN    PORT\n1       1/1\n10      1/2"


def test_getter_ignores_log_messages(driver):
    """A log message in the middle of a table does not change the result."""
    driver.device.current_test = "test_get_vlans"
    driver.device.current_test_case = "fabric_engine_9.4"
    expected = driver.get_vlans()

    original = driver.device.send_command

    def noisy(command, **kwargs):
        lines = original(command, **kwargs).splitlines()
        lines.insert(len(lines) // 2, LOG_LINE)
        return "\n".join(lines)

    driver.device.send_command = noisy
    assert driver.get_vlans() == expected


@pytest.mark.parametrize(
    "line",
    [
        # real output (Fabric Engine 9.4 VM), after a config mode prompt and on its own
        "5520-24T-FabricEngine:1(config)#1 2026-11-17T16:19:52.492Z 5520-24T-FabricEngine "
        "CP1 - 0x00088544 - 00000000 Mgmt SW INFO ZTP+ discovery started",
        "1 2026-11-17T16:19:52.495Z 5520-24T-FabricEngine CP1 - 0x00088544 - 00000000 "
        "Mgmt SW INFO ZTP+ discovery failed -  No DNS server configured.",
        f"{PROMPT}{LOG_LINE}",
        LOG_LINE,
    ],
)
def test_log_message_formats(line):
    from napalm_fe.fe import LOG_MESSAGE_RE

    assert LOG_MESSAGE_RE.fullmatch(line)


def test_log_message_in_the_middle_of_a_line():
    """A message that interrupts a line is removed and the line is joined again."""
    from napalm_fe.fe import FEDriver

    row = "1/1      192   10GbNone         true  false    1950  00:51:00:ce:20:00 up     up"
    split = row.index("1950") + 4  # interrupted right after a number
    output = f"PORT\n{row[:split]}{LOG_LINE}\n{row[split:]}\n1/2      193"
    assert FEDriver._send_command_postprocess(output) == f"PORT\n{row}\n1/2      193"


def test_config_lines_are_not_log_messages():
    from napalm_fe.fe import LOG_MESSAGE_RE

    for line in (
        'snmp-server location "DC1"',
        "ntp server 192.0.2.21",
        "1 out of 1 entries displayed",
        "10      1/2",
    ):
        assert not LOG_MESSAGE_RE.search(line)


# Real output of "show logging file event-code 0x0003068e" (Fabric Engine 9.4 VM)
LOG_FILE_OUTPUT = """\
************************************************************************************
                Command Execution Time: Tue Nov 17 16:37:13 2026 UTC
************************************************************************************
1 2026-11-17T16:33:05.861Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 GlobalRouter SW INFO Scheduled reset enabled! Unit will reset in 0 days 0 hours 10 minutes!
1 2026-11-17T16:33:44.223Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 GlobalRouter SW INFO Scheduled reset enabled! Unit will reset in 0 days 0 hours 10 minutes!
"""


@pytest.mark.parametrize(
    "command",
    ["show logging file event-code 0x0003068e", "show logging file tail", "show log file"],
)
def test_log_display_commands_keep_log_lines(driver, command):
    driver.device.send_command = lambda cmd, **kw: LOG_FILE_OUTPUT
    output = driver.cli([command])[command]
    assert output.count("Scheduled reset enabled!") == 2


def test_other_commands_drop_log_lines(driver):
    driver.device.send_command = lambda cmd, **kw: f"VLAN    PORT\n{LOG_LINE}\n1       1/1"
    assert driver.cli(["show vlan members"])["show vlan members"] == "VLAN    PORT\n1       1/1"


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


# commit confirm (revert_in), like Site Engine: scheduled reset


@pytest.fixture
def reboot_driver():
    driver = PatchedFEDriver("host", "user", "pass", optional_args={"allow_reboot": True})
    driver.device.current_test = "test_get_config"
    driver.device.current_test_case = "fabric_engine_9.4"
    return driver


LOG_QUERIES = [
    "show logging file event-code 0x0003068e",
    "show logging file event-code 0x0003068f",
    "show logging file event-code 0x00034594",
]


@pytest.mark.parametrize(
    "seconds, expected",
    [(60, "1"), (300, "5"), (359, "5"), (3600, "01:00"), (5400, "01:30"), (90000, "1:01:00")],
)
def test_revert_time(seconds, expected):
    from napalm_fe.fe import FEDriver

    assert FEDriver._revert_time(seconds) == expected


@pytest.mark.parametrize("seconds", [0, 59, 10 * 86400])
def test_revert_time_out_of_range(seconds):
    from napalm.base.exceptions import CommitConfirmException

    from napalm_fe.fe import FEDriver

    with pytest.raises(CommitConfirmException):
        FEDriver._revert_time(seconds)


def test_commit_with_revert_and_confirm(reboot_driver):
    device = reboot_driver.device
    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    reboot_driver.commit_config(revert_in=300)
    # saved before the change, reset scheduled before applying, not saved after
    assert device.commands == [
        *LOG_QUERIES,  # no other revert pending
        "save config",
        "reset -in 5",
        "y",
        *LOG_QUERIES,  # until the log shows the scheduled reset
    ]
    assert device.sent_config == ["ntp server 192.0.2.99"]
    assert device.scheduled_reset == "5"
    assert reboot_driver.has_pending_commit()

    reboot_driver.confirm_commit()
    assert device.commands[-5:] == ["reset -cancel", *LOG_QUERIES, "save config"]
    assert device.scheduled_reset is None
    assert not reboot_driver.has_pending_commit()


def test_confirm_after_the_switch_reverted(reboot_driver):
    from napalm.base.exceptions import CommitConfirmException

    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    reboot_driver.commit_config(revert_in=60)
    reboot_driver.device.scheduled_reset = None  # the reset already happened
    with pytest.raises(CommitConfirmException, match="No ongoing scheduled reset"):
        reboot_driver.confirm_commit()
    assert "save config" not in reboot_driver.device.commands[-1:]


def test_no_new_commit_while_pending(reboot_driver):
    from napalm.base.exceptions import CommitConfirmException

    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    reboot_driver.commit_config(revert_in=300)
    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.98")
    with pytest.raises(CommitConfirmException):
        reboot_driver.commit_config()


def test_revert_requires_allow_reboot(driver):
    from napalm.base.exceptions import CommitConfirmException

    driver.load_merge_candidate(config="ntp server 192.0.2.99")
    with pytest.raises(CommitConfirmException, match="allow_reboot"):
        driver.commit_config(revert_in=300)
    assert driver.device.sent_config == []
    assert driver.device.scheduled_reset is None


def test_confirm_without_pending_commit(driver):
    from napalm.base.exceptions import CommitConfirmException

    with pytest.raises(CommitConfirmException, match="No pending"):
        driver.confirm_commit()


def test_confirm_with_unexpected_cancel_output(reboot_driver):
    from napalm.base.exceptions import CommitConfirmException

    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    reboot_driver.commit_config(revert_in=300)
    reboot_driver.device.send_command_timing = lambda command, **kw: "something else"
    with pytest.raises(CommitConfirmException, match="cancelled"):
        reboot_driver.confirm_commit()
    assert reboot_driver.device.commands[-1] != "save config"  # nothing saved


SCHEDULED = """\
1 2026-11-17T16:33:05.861Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 GlobalRouter SW INFO Scheduled reset enabled! Unit will reset in 0 days 0 hours 10 minutes!
1 2026-11-17T16:33:44.223Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 GlobalRouter SW INFO Scheduled reset enabled! Unit will reset in 0 days 0 hours 10 minutes!
1 2026-11-17T16:34:55.339Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 GlobalRouter SW INFO Scheduled reset enabled! Unit will reset in 0 days 0 hours 10 minutes!
1 2026-11-17T16:36:19.303Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 GlobalRouter SW INFO Scheduled reset enabled! Unit will reset in 0 days 0 hours 10 minutes!
"""


CANCELED = """\
1 2026-11-17T16:33:14.081Z 5520-24T-FabricEngine CP1 - 0x0003068f - 00000000 GlobalRouter SW INFO Scheduled reset canceled!
1 2026-11-17T16:34:17.781Z 5520-24T-FabricEngine CP1 - 0x0003068f - 00000000 GlobalRouter SW INFO Scheduled reset canceled!
1 2026-11-17T16:35:36.193Z 5520-24T-FabricEngine CP1 - 0x0003068f - 00000000 GlobalRouter SW INFO Scheduled reset canceled!
"""


BOOT = """\
1 2026-11-17T15:51:41.628Z 5520-24T-FabricEngine CP1 - 0x00034594 - 00000000 GlobalRouter SW INFO System boot
"""


def _log_switch(driver, scheduled, canceled, boot):
    outputs = {"0x0003068e": scheduled, "0x0003068f": canceled, "0x00034594": boot}
    driver.device.send_command = lambda cmd, **kw: outputs[cmd.split()[-1]]


def test_pending_from_real_log(driver):
    # the newest "enabled" (16:36:19) is newer than the newest cancel (16:35:36)
    _log_switch(driver, SCHEDULED, CANCELED, BOOT)
    assert driver.has_pending_commit()


def test_not_pending_after_cancel(driver):
    cancel = CANCELED + CANCELED.splitlines()[-1].replace("16:35:36", "16:37:00") + "\n"
    _log_switch(driver, SCHEDULED, cancel, BOOT)
    assert not driver.has_pending_commit()


def test_not_pending_after_reboot(driver):
    # the timer expired: the switch rebooted after the reset was scheduled
    boot = BOOT.replace("15:51:41", "16:46:30")
    _log_switch(driver, SCHEDULED, CANCELED, boot)
    assert not driver.has_pending_commit()


def test_not_pending_without_entries(driver):
    _log_switch(driver, "", "", BOOT)
    assert not driver.has_pending_commit()


def test_confirm_commit_of_another_session(reboot_driver):
    """A pending revert scheduled by another session is found in the log."""
    device = reboot_driver.device
    device.scheduled_reset = "5"
    device._log("0x0003068e", "GlobalRouter SW INFO Scheduled reset enabled! ...")
    assert not reboot_driver.pending_revert
    reboot_driver.confirm_commit()
    assert device.commands[-5:] == ["reset -cancel", *LOG_QUERIES, "save config"]


def test_latest_log_time():
    from datetime import datetime

    from napalm_fe import parsers

    assert parsers.latest_log_time(SCHEDULED) == datetime(2026, 11, 17, 16, 36, 19, 303000)
    assert parsers.latest_log_time("") is None


def test_not_pending_after_real_cancel(driver):
    # real output a few minutes later: cancelled at 16:38:16, after the last enable
    canceled = CANCELED + (
        "1 2026-11-17T16:38:16.606Z 5520-24T-FabricEngine CP1 - 0x0003068f - 00000000 "
        "GlobalRouter SW INFO Scheduled reset canceled!\n"
    )
    _log_switch(driver, SCHEDULED, canceled, BOOT)
    assert not driver.has_pending_commit()


def test_not_pending_after_real_revert_reboot(driver):
    """Real outputs after a 'reset -in 1' ran out: the VM's log starts empty after
    the reboot, apart from the new 'System boot' entry."""
    banner = (
        "*" * 84
        + "\n                Command Execution Time: Tue Nov 17 16:47:12 2026 UTC\n"
        + "*" * 84
    )
    boot = banner + (
        "\n1 2026-11-17T16:42:58.329Z 5520-24T-FabricEngine CP1 - 0x00034594 - 00000000 "
        "GlobalRouter SW INFO System boot"
    )
    _log_switch(driver, banner, banner, boot)
    assert not driver.has_pending_commit()


def test_commit_confirm_waits_for_the_log(reboot_driver, monkeypatch):
    """The switch logs a scheduled reset a few seconds late (9.4, API)."""
    from napalm_fe import fe

    real = reboot_driver.has_pending_commit
    # the pre-commit check, then the log is behind twice, then the real state
    answers = iter([False, False, False])
    monkeypatch.setattr(reboot_driver, "has_pending_commit", lambda: next(answers, real()))
    sleeps = []
    monkeypatch.setattr(fe.time, "sleep", sleeps.append)
    reboot_driver.load_merge_candidate(config="foo")
    reboot_driver.commit_config(revert_in=300)
    assert sleeps == [1, 1]


def test_log_wait_gives_up(reboot_driver, monkeypatch, caplog):
    from napalm_fe import fe

    monkeypatch.setattr(reboot_driver, "has_pending_commit", lambda: False)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(fe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(fe.time, "sleep", lambda s: None)
    reboot_driver._wait_for_log(pending=True)
    assert "does not show the change yet" in caplog.text
