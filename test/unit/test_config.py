"""Tests for the configuration management workflow."""

import re

import pytest
from napalm.base.exceptions import MergeConfigException, ReplaceConfigException
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
    """Says that nothing was rolled back and how to restore (like napalm-dellos10)."""
    driver.device.send_config_set = lambda lines, **kw: COMMUNITY_EXISTS
    driver.load_merge_candidate(config="foo")
    with pytest.raises(MergeConfigException) as exc:
        driver.commit_config()
    message = str(exc.value)
    assert "automatic rollback not available" in message
    assert "/intflash/napalm_rollback.cfg" in message
    assert "allow_reboot=True" in message
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


# config_method="file": SCP upload and "source <file> debug"


class FakeTransfer:
    """Replaces the SCP transfer: stores the file in the fake switch's flash."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        with open(kwargs["source_file"]) as handle:
            self.content = handle.read()
        path = f"{kwargs['file_system']}/{kwargs['dest_file']}"
        kwargs["ssh_conn"].flash[path] = self.content
        FakeTransfer.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def verify_space_available(self):
        return True

    def transfer_file(self):
        pass

    def verify_file(self):
        return True


def _make_driver(monkeypatch, **optional_args):
    from napalm_fe import fe

    FakeTransfer.instances = []
    monkeypatch.setattr(fe, "ExtremeVspFileTransfer", FakeTransfer)
    driver = PatchedFEDriver(
        "host", "user", "pass", optional_args={"transport": "ssh", **optional_args}
    )
    # compare_config() reads the running config of the real Fabric Engine 9.4 capture
    driver.device.current_test = "test_get_config"
    driver.device.current_test_case = "fabric_engine_9.4"
    return driver


@pytest.fixture
def file_driver(monkeypatch):
    return _make_driver(monkeypatch, config_method="file")


def test_file_method_upload_and_commit(file_driver):
    file_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    upload = FakeTransfer.instances[0]
    assert upload.kwargs["dest_file"] == "napalm_merge.cfg"
    assert upload.kwargs["file_system"] == "/intflash"
    assert upload.content == "config terminal\nntp server 192.0.2.99\nend\n"

    assert file_driver.compare_config() == "+ntp server 192.0.2.99"
    file_driver.commit_config()
    assert file_driver.device.commands[-3:] == [
        "save config file /intflash/napalm_rollback.cfg",
        "source /intflash/napalm_merge.cfg debug",
        "save config",
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
        PatchedFEDriver("host", "user", "pass", optional_args={"config_method": "x"})


SOURCE_OUTPUT = """\
1 2026-11-17T16:28:54.149Z 5520-24T-FabricEngine CP1 - 0x001dc70e - 00000000 GlobalRouter ISIS INFO Loading ISIS Md5 config key file with size 0 bytes.
1 2026-11-17T16:28:54.149Z 5520-24T-FabricEngine CP1 - 0x001dc70e - 00000000 GlobalRouter ISIS INFO Loading ISIS simple config key file with size 0 bytes.
1 2026-11-17T16:28:54.149Z 5520-24T-FabricEngine CP1 - 0x001dc70e - 00000000 GlobalRouter ISIS INFO Loading ISIS sha256 config key file with size 0 bytes.
"""


def test_file_method_real_source_output(file_driver):
    from napalm_fe.fe import FEDriver

    assert FEDriver._send_command_postprocess(SOURCE_OUTPUT) == ""
    original = file_driver.device.send_command

    def send_command(command, **kwargs):
        if command.startswith("source "):
            file_driver.device.commands.append(command)
            return SOURCE_OUTPUT
        return original(command, **kwargs)

    file_driver.device.send_command = send_command
    file_driver.load_merge_candidate(config='snmp-server location "napalm-test"')
    file_driver.commit_config()
    assert file_driver.device.commands[-2:] == [
        "source /intflash/napalm_merge.cfg debug",
        "save config",
    ]


SOURCE_DEBUG_TYPO = """\

5520-24T-FabricEngine:1#config terminal
Enter configuration commands, one per line.  End with CNTL/Z.
5520-24T-FabricEngine:1(config)#snmp-server location "napalm-test"
5520-24T-FabricEngine:1(config)#asdasda
                                 ^
% Invalid input detected at '^' marker.
5520-24T-FabricEngine:1(config)#end
"""


SOURCE_DEBUG_GOOD = """\

5520-24T-FabricEngine:1#config terminal
Enter configuration commands, one per line.  End with CNTL/Z.
5520-24T-FabricEngine:1(config)#snmp-server location "napalm-test"
5520-24T-FabricEngine:1(config)#end
"""


@pytest.mark.parametrize("output, fails", [(SOURCE_DEBUG_TYPO, True), (SOURCE_DEBUG_GOOD, False)])
def test_file_method_source_debug_output(file_driver, output, fails):
    original = file_driver.device.send_command

    def send_command(command, **kwargs):
        if command.startswith("source "):
            # the echo contains the prompt: only the prompt at the end may match
            assert not re.search(kwargs["expect_string"], output)
            assert re.search(kwargs["expect_string"], output + "5520-24T-FabricEngine:1#")
            return output
        return original(command, **kwargs)

    file_driver.device.send_command = send_command
    file_driver.load_merge_candidate(config='snmp-server location "napalm-test"\nasdasda')
    if fails:
        with pytest.raises(MergeConfigException, match="Invalid input"):
            file_driver.commit_config()
        assert "save config" not in file_driver.device.commands
    else:
        file_driver.commit_config()
        assert file_driver.device.commands[-1] == "save config"


def test_upload_stops_if_free_space_unknown(monkeypatch):
    """The upload is refused (no transfer) if 'dir' output cannot be read."""

    class NoSpaceInfo(FakeTransfer):
        def verify_space_available(self):
            raise ValueError("Free space not found in the output of 'dir'")

        def transfer_file(self):
            raise AssertionError("must not transfer")

    from napalm_fe import fe

    driver = _make_driver(monkeypatch, config_method="file")
    monkeypatch.setattr(fe, "ExtremeVspFileTransfer", NoSpaceInfo)
    with pytest.raises(MergeConfigException, match="Free space not found"):
        driver.load_merge_candidate(config="ntp server 192.0.2.99")


def test_discard_merge_deletes_nothing(file_driver):
    file_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    file_driver.discard_config()
    assert not any(c.startswith("delete") for c in file_driver.device.commands)


# replace and rollback(): boot configuration and reboot


FULL_CONFIG = """\
config terminal
prompt "fe-sw-013"
ntp server 192.0.2.21
end
"""


@pytest.fixture
def reboot_driver(monkeypatch):
    return _make_driver(monkeypatch, allow_reboot=True)


def test_replace_compare_and_commit(reboot_driver):
    device = reboot_driver.device
    reboot_driver.load_replace_candidate(config=FULL_CONFIG)
    upload = FakeTransfer.instances[0]
    assert upload.kwargs["dest_file"] == "napalm_candidate.cfg"
    assert upload.content == FULL_CONFIG

    diff = reboot_driver.compare_config().splitlines()
    assert "-ntp server 192.0.2.22" in diff
    assert "-sys mtu 9600" in diff
    assert not any(line.startswith("+") for line in diff)  # nothing new in the candidate

    reboot_driver.commit_config()
    assert device.commands == [
        "show running-config",
        "save config file /intflash/napalm_rollback.cfg",
        "dir /intflash -l",
        "copy /intflash/napalm_candidate.cfg /intflash/config.cfg",
        "y",
        "reset -y",
    ]
    assert device.flash["/intflash/config.cfg"] == FULL_CONFIG
    assert device.rebooted
    assert reboot_driver.compare_config() == ""  # candidate consumed


def test_replace_requires_allow_reboot(monkeypatch):
    driver = _make_driver(monkeypatch)
    driver.load_replace_candidate(config=FULL_CONFIG)
    with pytest.raises(ReplaceConfigException, match="allow_reboot"):
        driver.commit_config()
    assert not driver.device.rebooted
    assert driver.device.flash["/intflash/config.cfg"] == "startup config"


def test_replace_from_missing_file(reboot_driver):
    with pytest.raises(ReplaceConfigException):
        reboot_driver.load_replace_candidate(filename="/does/not/exist")
    assert reboot_driver.compare_config() == ""


def test_discard_replace(reboot_driver):
    reboot_driver.load_replace_candidate(config=FULL_CONFIG)
    assert "/intflash/napalm_candidate.cfg" in reboot_driver.device.flash
    reboot_driver.discard_config()
    # like NX-OS: the uploaded candidate is deleted on the switch
    assert reboot_driver.device.commands[-2:] == ["delete /intflash/napalm_candidate.cfg", "y"]
    assert "/intflash/napalm_candidate.cfg" not in reboot_driver.device.flash
    assert reboot_driver.compare_config() == ""
    with pytest.raises(MergeConfigException):
        reboot_driver.commit_config()


def test_rollback_after_merge(reboot_driver):
    device = reboot_driver.device
    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    reboot_driver.commit_config()
    device.commands.clear()

    reboot_driver.rollback()
    assert device.commands == [
        "dir /intflash -l",
        "copy /intflash/napalm_rollback.cfg /intflash/config.cfg",
        "y",
        "reset -y",
    ]
    assert device.flash["/intflash/config.cfg"] == "running config"
    assert device.rebooted


def test_rollback_without_rollback_file(reboot_driver):
    with pytest.raises(ReplaceConfigException, match="does not exist"):
        reboot_driver.rollback()
    assert not reboot_driver.device.rebooted


def test_rollback_requires_allow_reboot(driver):
    with pytest.raises(ReplaceConfigException, match="allow_reboot"):
        driver.rollback()
    assert not driver.device.rebooted


def test_confirm_prompt_followed_by_log_message(reboot_driver):
    device = reboot_driver.device
    original = device.send_command_timing

    def noisy(command, **kwargs):
        output = original(command, **kwargs)
        return f"{output}\n{LOG_LINE}" if output else output

    device.send_command_timing = noisy
    reboot_driver.load_merge_candidate(config="ntp server 192.0.2.99")
    reboot_driver.commit_config()
    reboot_driver.rollback()
    assert device.rebooted  # both (y/n) prompts were answered


def test_discard_survives_failed_delete(reboot_driver):
    reboot_driver.load_replace_candidate(config=FULL_CONFIG)

    def broken(command, **kwargs):
        raise OSError("Socket is closed")

    reboot_driver.device.send_command_timing = broken
    reboot_driver.discard_config()  # logged, not raised
    assert reboot_driver.compare_config() == ""


def test_merge_saves_rollback_file_first(driver):
    driver.load_merge_candidate(config="ntp server 192.0.2.99")
    driver.commit_config()
    assert driver.device.commands[0] == "save config file /intflash/napalm_rollback.cfg"
    assert "/intflash/napalm_rollback.cfg" in driver.device.flash
    assert driver.device.sent_config == ["ntp server 192.0.2.99"]


def test_second_commit_overwrites_rollback_file(driver):
    # the rollback file exists from the first commit: the switch asks before
    # overwriting it (Fabric Engine 9.3) and aborts on any answer but "y"
    driver.load_merge_candidate(config="ntp server 192.0.2.99")
    driver.commit_config()
    driver.load_merge_candidate(config="ntp server 192.0.2.98")
    driver.commit_config()
    assert driver.device.commands.count("save config file /intflash/napalm_rollback.cfg") == 2
    assert driver.device.commands.count("y") == 1
    assert driver.device.sent_config == ["ntp server 192.0.2.99", "ntp server 192.0.2.98"]


def test_merge_aborts_if_rollback_file_fails(driver):
    driver.device.send_command = lambda command, **kw: "Error: flash is full"
    driver.load_merge_candidate(config="ntp server 192.0.2.99")
    with pytest.raises(
        MergeConfigException,
        match="Could not save the configuration to /intflash/napalm_rollback.cfg",
    ):
        driver.commit_config()
    assert driver.device.sent_config == []
