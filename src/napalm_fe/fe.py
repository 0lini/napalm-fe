"""NAPALM driver for Extreme Networks Fabric Engine (formerly VOSS / VSP).

The default transport (``ssh``, like NAPALM's IOS driver) is the CLI over
Netmiko (``extreme_vsp``); it works on every release. ``transport="https"``
uses the switch's API (OpenAPI, Fabric Engine 9.3.1 or later with a Premier
license); like NAPALM's NX-OS driver, an SSH connection is then only opened
for what the API cannot do: uploading files (SCP).
"""

import logging
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Union

from napalm.base import NetworkDriver, models
from napalm.base.exceptions import (
    CommandErrorException,
    CommitConfirmException,
    ConnectionClosedException,
    ConnectionException,
    MergeConfigException,
    ReplaceConfigException,
)
from napalm.base.helpers import sanitize_configs
from napalm.base.netmiko_helpers import netmiko_args
from netmiko.exceptions import ConfigInvalidException

from napalm_fe import openapi_parsers, parsers
from napalm_fe.file_transfer import ExtremeVspFileTransfer, parse_dir
from napalm_fe.openapi import DEFAULT_PORT as DEFAULT_HTTPS_PORT
from napalm_fe.openapi import OpenApiSession

logger = logging.getLogger(__name__)

SUPPORTED_TRANSPORTS = ("https", "ssh")

# Like the NX-OS driver, the SSH connection the https transport opens for file
# uploads always uses port 22 ("port" is the API port).
SSH_PORT = 22

# Extreme renamed VOSS (VSP Operating System Software) to Fabric Engine;
# Netmiko still calls it "extreme_vsp".
NETMIKO_DEVICE_TYPE = "extreme_vsp"

# Log messages printed into the session when "logging screen" is enabled
# (a switch-wide setting the driver does not change), RFC 5424 format (seen
# on the serial console and in SSH sessions):
#   1 2026-11-17T16:11:12.756Z <host> CP1 - 0x000305ca - 00000000 GlobalRouter SW INFO ...
# They arrive asynchronously: on their own line, right after a prompt in any
# mode ("switch:1>", "switch:1(config)#") or in the middle of a line of
# output. The message and its line break are removed, which also joins a line
# that was split by a message. The RFC 5424 version is always "1", so digits
# right before a message are not taken as part of it.
LOG_MESSAGE_RE = re.compile(
    r"(?:^\S+:\d+(?:\([^)\s]*\))?[>#])?"
    r"1 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}) "
    r"\S+ CP\d+ - 0x[0-9a-fA-F]+ - [^\n]*\n?",
    re.M,
)

# Commands that display the log: their output consists of log messages, so the
# filter above must not be applied to them.
LOG_DISPLAY_COMMAND_RE = re.compile(r"^\s*show\s+log", re.I)

# Log event codes (Fabric Engine 9.4) used to find a scheduled reset
EVENT_RESET_SCHEDULED = "0x0003068e"  # Scheduled reset enabled! Unit will reset in ...
EVENT_RESET_CANCELED = "0x0003068f"  # Scheduled reset canceled!
EVENT_SYSTEM_BOOT = "0x00034594"  # System boot

# The switch writes these log entries a few seconds after the command (9.4:
# up to ~5 s through the API); seconds to wait until the log shows the change.
LOG_DELAY = 30

# Output that shows that the CLI rejected a command. Fabric Engine 9.4 also
# reports errors as e.g. "Error: The community entry already exists." (no
# "%"), sometimes after a BEL ("\x07Error: The specified VLAN ID is invalid or
# already in use", OpenAPI CLI operation). Also passed to Netmiko's
# send_config_set(error_pattern=...), which applies it with re.M as well.
CLI_ERROR_RE = re.compile(
    r"% (?:Invalid input|Incomplete command|Ambiguous command|Unrecognized command|Cannot |Error)"
    r"|^[\s\x07]*Error\s*:",
    re.M,
)

# NAPALM privilege levels the switch can create users with (username add /
# POST configuration/accounts accept only ro, rw and rwa; Fabric Engine 9.4)
USER_CREATE_LEVELS = {1: "ro", 14: "rw", 15: "rwa"}
API_ACCOUNT_LEVELS = {
    "ro": "READ_ONLY",
    "l1": "L1_LIMITED",
    "l2": "L2_LIMITED",
    "l3": "L3_LIMITED",
    "rw": "ADMIN_LIMITED",
    "rwa": "ADMIN",
}
# access level -> "cli password <user> <level>" keyword
CLI_PASSWORD_LEVELS = {
    "ro": "read-only",
    "l1": "layer1",
    "l2": "layer2",
    "l3": "layer3",
    "rw": "read-write",
    "rwa": "read-write-all",
}
USER_TEMPLATES = ("set_users", "delete_users")
DEFAULT_ACCOUNTS = ("ro", "l1", "l2", "l3", "rw", "rwa")

# Lines of "show running-config" / the saved config file that change on every
# call; removed so that get_config() output is stable (like the IOS driver).
CONFIG_VOLATILE_LINES = (
    r"^\*+[ \t]*$",
    r"^[ \t]*Command Execution Time:.*$",
    r"^Preparing to Display Configuration.*$",
    r"^# \w{3} \w{3} +\d+ \d+:\d+:\d+ \d{4}.*$",
)

# Filters for get_config(sanitized=True). They only match within a line
# ([ \t] instead of \s) so that they never swallow the following line.
SANITIZE_FILTERS = {
    r'^([ \t]*snmp-server community[ \t]+)"?[^"\s]+"?': r"\1<removed>",
    r'^([ \t]*ntp authentication-key[ \t]+\d+[ \t]+\S+[ \t]+)"?[^"\s]+"?': r"\1<removed>",
    r'((?<![-\w])key[ \t]+)"?[^"\s]+"?': r"\1<removed>",
    r'(\bsecret[ \t]+)"?[^"\s]+"?': r"\1<removed>",
    # "password hash sha2" is a setting, not a secret
    r'(\bpassword[ \t]+)(?!hash\b)"?[^"\s]+"?': r"\1<removed>",
    # SNMPv3 user auth / privacy passwords
    r"^([ \t]*snmp-server user\b.*?\b(?:md5|sha|sha2|sha256|sha384|sha512)[ \t]+)\S+": (
        r"\1<removed>"
    ),
    r"^([ \t]*snmp-server user\b.*?\b(?:aes|aes192|aes256|des|3des)[ \t]+)\S+": r"\1<removed>",
}


class FEDriver(NetworkDriver):
    """NAPALM driver for Extreme Networks Fabric Engine."""

    platform = "fe"

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        timeout: int = 60,
        optional_args: Optional[Dict] = None,
    ) -> None:
        """Constructor.

        Supported ``optional_args`` (in addition to the Netmiko arguments such
        as ``port``, ``secret``, ``key_file``, ``ssh_config_file``...):

        * ``transport`` (default ``"ssh"``): how the driver talks to the
          switch, like NAPALM's EOS and NX-OS drivers. ``"ssh"``: CLI via
          Netmiko, every release. ``"https"``: the switch's OpenAPI (Fabric
          Engine 9.3.1 or later, Premier license); SSH (port 22, like NX-OS)
          is opened on first use for file uploads (SCP).
        * ``port``: port of the transport (default ``22`` for ``ssh``,
          ``9443`` for ``https``).
        * ``ssl_verify`` (default ``False``, like NX-OS): check the API's TLS
          certificate; ``True`` or the path of a CA bundle.
        * ``save_config`` (bool, default ``True``): run ``save config`` after
          a successful ``commit_config``.
        * ``startup_config_file`` (str, default ``/intflash/config.cfg``):
          the file returned as the ``startup`` configuration by ``get_config``.
        * ``config_method`` (``"cli"`` or ``"file"``, default ``"cli"``): how
          ``commit_config`` applies a merge candidate. ``"cli"`` sends the lines
          in configuration mode. ``"file"`` works like the IOS driver: the
          candidate is copied to the switch with SCP on load and applied with
          ``source``.
        * ``allow_reboot`` (bool, default ``False``): allow replace commits and
          ``rollback()``. Fabric Engine has no ``configure replace``; like
          ExtremeCloud IQ Site Engine's configuration restore, the driver
          installs the file as the boot configuration and reboots the switch.
        * ``dest_file_system`` (default ``/intflash``), ``candidate_cfg``
          (default ``napalm_candidate.cfg``), ``merge_cfg`` (default
          ``napalm_merge.cfg``) and ``rollback_cfg`` (default
          ``napalm_rollback.cfg``): files on the switch. Like the IOS driver, the
          running configuration is saved to ``rollback_cfg`` before every
          commit.
        """
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        optional_args = optional_args or {}

        self.transport = optional_args.get("transport", "ssh")
        if self.transport not in SUPPORTED_TRANSPORTS:
            raise ConnectionException(f"Unknown transport: {self.transport}")
        self.save_on_commit = bool(optional_args.get("save_config", True))
        self.startup_config_file = optional_args.get("startup_config_file", "/intflash/config.cfg")
        self.force_no_enable = bool(optional_args.get("force_no_enable", False))
        self.config_method = optional_args.get("config_method", "cli")
        if self.config_method not in ("cli", "file"):
            raise ValueError("config_method must be 'cli' or 'file'")
        self.dest_file_system = optional_args.get("dest_file_system", "/intflash").rstrip("/")
        self.candidate_cfg = optional_args.get("candidate_cfg", "napalm_candidate.cfg")
        self.merge_cfg = optional_args.get("merge_cfg", "napalm_merge.cfg")
        self.allow_reboot = bool(optional_args.get("allow_reboot", False))
        self.rollback_cfg = optional_args.get("rollback_cfg", "napalm_rollback.cfg")
        self.netmiko_optional_args = netmiko_args(optional_args)
        self.ssl_verify: Union[bool, str] = optional_args.get("ssl_verify", False)
        if self.transport == "https":
            self.port = int(optional_args.get("port", DEFAULT_HTTPS_PORT))
            if "port" in self.netmiko_optional_args:
                self.netmiko_optional_args["port"] = SSH_PORT
        else:
            self.port = int(optional_args.get("port", SSH_PORT))

        self.device: Any = None
        self.api: Optional[OpenApiSession] = None
        self.candidate_config: Optional[str] = None
        self.config_replace = False
        self.pending_revert = False
        # pending set_users / delete_users: [("add", name, level, password) | ("delete", name)]
        self.user_changes: List[tuple] = []
        self.profile = [self.platform]

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open a connection to the device.

        With ``transport="https"`` only the API session is opened (it gets a
        token); SSH follows when something needs it (see ``_ssh``).
        """
        if self.transport == "https":
            self.api = OpenApiSession(
                self.hostname,
                self.username,
                self.password,
                port=self.port,
                verify=self.ssl_verify,
                timeout=self.timeout,
            )
            self.api.open()
            return
        self._ssh_open()

    def _ssh_open(self) -> None:
        self.device = self._netmiko_open(
            NETMIKO_DEVICE_TYPE, netmiko_optional_args=self.netmiko_optional_args
        )

    def close(self) -> None:
        """Close the connection(s) to the device."""
        if self.api is not None:
            self.api.close()
            self.api = None
        if self.device is not None:
            self._netmiko_close()
            self.device = None

    def is_alive(self) -> models.AliveDict:
        """Return whether the connection is still usable.

        https: whether the session holds a token (no request, like the EOS and
        NX-OS drivers); ssh: whether the SSH transport is active.
        """
        if self.transport == "https":
            return {"is_alive": self.api is not None and self.api.token is not None}
        if self.device is None:
            return {"is_alive": False}
        try:
            # Send an ASCII NUL to keep the session alive, then ask the transport
            self.device.write_channel(chr(0))
            return {"is_alive": bool(self.device.remote_conn.transport.is_active())}
        except (OSError, EOFError, AttributeError):
            return {"is_alive": False}

    # ------------------------------------------------------------------
    # Transport primitives (like NAPALM's NX-OS drivers): everything that
    # talks to the API or to Netmiko is here.
    #
    # https transport: everything goes through the API except file uploads
    # (SCP, _upload_config), which need SSH, see _ssh().
    # ------------------------------------------------------------------

    def _api(self) -> OpenApiSession:
        if self.api is None:
            raise ConnectionClosedException("Not connected to the device, call open() first")
        return self.api

    def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET an OpenAPI path (below ``/rest/openapi``)."""
        return self._api().get(path, params)

    def _ssh(self) -> Any:
        """The Netmiko session; with the https transport opened on first use.

        Like the NX-OS driver's ``ensure_netmiko_conn``: same credentials,
        port 22. With https only SCP uploads need it.
        """
        if self.device is None and self.api is not None:
            self._ssh_open()
        if self.device is None:
            raise ConnectionClosedException("Not connected to the device, call open() first")
        return self.device

    def _send_command(self, command: str, **kwargs: Any) -> str:
        """Send a command and return its post-processed output.

        https: through ``POST /v0/operation/system/cli`` (same output as over
        SSH, without the ``Command Execution Time`` banner). ssh: Netmiko
        ``send_command``; ``kwargs`` are passed on.
        """
        if self.transport == "https":
            logger.debug("%s: sending %r (API)", self.hostname, command)
            output = self._api().cli([command])[0]
        else:
            device = self._ssh()
            # Large tables (running-config, FDB on a chassis) can take longer
            # than Netmiko's 10 second default; use the driver timeout instead.
            kwargs.setdefault("read_timeout", self.timeout)
            logger.debug("%s: sending %r", self.hostname, command)
            try:
                output = str(device.send_command(command, **kwargs))
            except (OSError, EOFError) as exc:
                raise ConnectionClosedException(str(exc)) from exc
        return self._send_command_postprocess(
            output, remove_log_messages=not LOG_DISPLAY_COMMAND_RE.match(command)
        )

    def _send_command_timing(self, command: str) -> str:
        """Send a command that prompts or has no fixed end (copy, delete, reset, y).

        https: through the CLI operation, which confirms (y/n) questions
        itself (Fabric Engine 9.4: ``copy`` overwrites an existing file and
        only echoes ``File [...] already existing,``; ``delete`` and ``reset
        -in`` print nothing), so ``_confirm`` never has to answer. Log messages
        printed into the session (they may follow a prompt) are removed.
        """
        if self.transport == "https":
            logger.debug("%s: sending %r (API)", self.hostname, command)
            output = self._api().cli([command])[0]
        else:
            output = str(self._ssh().send_command_timing(command, read_timeout=self.timeout))
        return LOG_MESSAGE_RE.sub("", output)

    def _send_config(self, lines: List[str]) -> str:
        """Send lines in configuration mode; stop at the first rejected line.

        Raises ``ConfigInvalidException`` (Netmiko) for a rejected line.
        """
        if self.transport == "https":
            return self._send_config_api(lines)
        try:
            return str(
                self._ssh().send_config_set(
                    lines, cmd_verify=False, error_pattern=CLI_ERROR_RE.pattern
                )
            )
        except ConfigInvalidException:
            try:
                self.device.exit_config_mode()
            except Exception:  # pragma: no cover - best effort
                pass
            raise

    def _send_config_api(self, lines: List[str]) -> str:
        """``_send_config`` through ``POST /v0/operation/system/cli``.

        Each request starts in privileged EXEC mode, hence ``configure
        terminal`` / ``end``. Without ``continue_on_failure`` the switch skips
        every command after the first one it counts as failed (any output
        containing "Error"), so it stops at a rejected line like Netmiko does
        with ``error_pattern`` (Fabric Engine 9.4).
        """
        commands = ["configure terminal", *lines, "end"]
        outputs = self._api().cli(commands, continue_on_failure=False)
        ran = list(zip(commands, outputs, strict=False))
        transcript = "\n".join(f"{c}\n{o}".rstrip() for c, o in ran)
        failed = next((c for c, o in ran if self._has_cli_error(o)), None)
        if failed:
            raise ConfigInvalidException(
                f"Invalid input detected at command: {failed}\n{transcript}"
            )
        if len(outputs) < len(commands):
            last = ran[-1][0] if ran else commands[0]
            raise ConfigInvalidException(
                f"The switch skipped the commands after: {last}\n{transcript}"
            )
        return transcript

    def _copy_run_start(self, exc_class: type = CommandErrorException) -> None:
        """Save the running configuration (``save config``)."""
        if self.transport == "https":
            try:
                self._api().save_config()
            except CommandErrorException as exc:
                raise exc_class(f"Saving the configuration failed: {exc}") from exc
            return
        output = str(self._ssh().save_config())
        if not self._save_successful(output):
            raise exc_class(f"Saving the configuration failed:\n{output}")

    def _save_config_file(self, path: str, exc_class: type = CommandErrorException) -> None:
        """Save the running configuration to ``path`` (``save config file``).

        https: ``POST /v0/operation/system/config/:save`` with ``name``; it
        overwrites an existing file without asking (Fabric Engine 9.4). ssh:
        if the file exists the switch asks
        ``File [/intflash/napalm_rollback.cfg] already existing, overwrite (y/n) ?``
        (Fabric Engine 9.3); any answer but ``y`` aborts the save. Saving takes
        a few seconds, so this waits for the prompt, not for the output to settle.
        """
        if self.transport == "https":
            try:
                self._api().save_config(path)
            except CommandErrorException as exc:
                raise exc_class(f"Could not save the configuration to {path}: {exc}") from exc
            return
        prompt = re.escape(self._ssh().find_prompt())
        output = self._send_command(f"save config file {path}", expect_string=rf"\(y/n\)|{prompt}")
        if re.search(r"\(y/n\)", output):
            output += "\n" + self._send_command("y", expect_string=prompt, cmd_verify=False)
        if not self._save_successful(output):
            raise exc_class(f"Could not save the configuration to {path}:\n{output}")

    def _source_file(self, path: str) -> str:
        """Run the configuration file ``path`` on the switch (``source <file> debug``).

        Without ``debug`` the switch prints nothing, not even for a rejected
        line; ``stop`` (stop on error) is silent as well and cannot be combined
        with ``debug`` (Fabric Engine 9.3). With ``debug`` every line is
        echoed after a prompt (``5520-24T-FabricEngine:1(config)#asdasda``)
        followed by its error (``% Invalid input detected at '^' marker.``);
        the following lines are still run over SSH. Through the API's CLI
        operation the output is the same, but ``source`` stops at the first
        rejected line (Fabric Engine 9.4). Over SSH, as the echo contains the
        prompt, this waits for the prompt at the end of the output.
        """
        read_timeout = max(self.timeout, 120)
        if self.transport == "https":
            output = self._api().cli([f"source {path} debug"], timeout=read_timeout)[0]
            return self._send_command_postprocess(output)
        prompt = re.escape(self._ssh().find_prompt())
        return self._send_command(
            f"source {path} debug", expect_string=rf"{prompt}\s*$", read_timeout=read_timeout
        )

    @staticmethod
    def _send_command_postprocess(output: str, remove_log_messages: bool = True) -> str:
        """Remove CLI noise from command output (like the IOS driver).

        Removes log messages printed into the session ("logging screen") -
        except for commands that display the log ("show logging ...") - and
        the "Please widen the terminal" hint. The "Command Execution Time"
        banner is kept: it is used to compute ``last_flapped``.
        """
        if remove_log_messages:
            output = LOG_MESSAGE_RE.sub("", output)
        output = re.sub(r"^Please widen the terminal.*$", "", output, flags=re.M)
        return output.strip()

    @staticmethod
    def _clean_config(output: str) -> str:
        """Remove banners and timestamps so configs can be compared / re-used."""
        pattern = "|".join(f"(?:{p})" for p in CONFIG_VOLATILE_LINES)
        output = re.sub(pattern, "", output, flags=re.M)
        return output.strip() + "\n" if output.strip() else ""

    @staticmethod
    def _has_cli_error(output: str) -> bool:
        return bool(CLI_ERROR_RE.search(output))

    # ------------------------------------------------------------------
    # CLI
    # ------------------------------------------------------------------

    def cli(
        self, commands: List[str], encoding: str = "text"
    ) -> Dict[str, Union[str, Dict[str, Any]]]:
        """Execute a list of commands and return the output keyed by command.

        Like the IOS driver, CLI errors are returned as output (not raised), so
        the caller sees the device's own error message.
        """
        if encoding != "text":
            raise NotImplementedError(f"{encoding} is not a supported encoding")
        if not isinstance(commands, list):
            raise TypeError("Please enter a valid list of commands!")
        result: Dict[str, Union[str, Dict[str, Any]]] = {}
        for command in commands:
            result[command] = self._send_command(command)
        return result

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_facts(self) -> models.FactsDict:
        """Return a set of facts about the device."""
        if self.transport == "https":
            return openapi_parsers.facts(  # type: ignore[return-value]
                self._api_get("/v0/state/system"),
                self._api_get("/v0/configuration/dns"),
                self._api_get("/v1/state/ports"),
            )
        sys_info = parsers.parse_sys_info(self._send_command("show sys-info"))
        interfaces = parsers.parse_interfaces(self._send_command("show interfaces gigabitEthernet"))

        hostname = sys_info.get("SysName", "")
        model = sys_info.get("ModelName") or sys_info.get("Chassis", "")
        domain = parsers.parse_domain_name(
            self._send_command("show running-config | include domain-name")
        )
        return {
            "hostname": hostname,
            "fqdn": f"{hostname}.{domain}" if hostname and domain else hostname,
            "vendor": "Extreme Networks",
            "model": model,
            "serial_number": sys_info.get("Serial#", ""),
            "os_version": parsers.parse_os_version(sys_info.get("SysDescr", "")),
            "uptime": parsers.parse_uptime(sys_info.get("SysUpTime", "")),
            "interface_list": list(interfaces.keys()),
        }

    def get_environment(self) -> models.EnvironmentDict:
        """Return fans, temperatures, power supplies, CPU and memory.

        CPU is reported per slot (current utilization), memory in bytes summed
        over all slots (``available_ram`` is the total, used + free).
        """
        output = self._send_command("show sys-info")
        cpu_output = self._send_command("show khi performance cpu")
        memory_output = self._send_command("show khi performance memory")
        return {
            "fans": parsers.parse_sys_info_fans(output),
            "temperature": parsers.parse_sys_info_temperature(output),
            "power": parsers.parse_sys_info_power(output),
            "cpu": parsers.parse_khi_cpu(cpu_output),
            "memory": parsers.parse_khi_memory(memory_output),
        }

    def get_interfaces(self) -> Dict[str, models.InterfaceDict]:
        """Return the physical interfaces of the device."""
        output = self._send_command("show interfaces gigabitEthernet")
        interfaces = parsers.parse_interfaces(output)
        names = parsers.parse_interfaces_name(output)
        now = parsers.command_execution_time(output)
        if now is None:  # no banner (https transport): the switch's clock
            now = parsers.parse_clock(self._send_command("show clock"))
        state_changes = parsers.parse_port_state(output)

        result: Dict[str, models.InterfaceDict] = {}
        for port, data in interfaces.items():
            name = names.get(port, {})
            last_flapped = -1.0
            if now and port in state_changes:
                last_flapped = max((now - state_changes[port]).total_seconds(), 0.0)
            result[port] = {
                "is_up": data["is_up"],
                "is_enabled": data["is_enabled"],
                "description": name.get("name", ""),
                "last_flapped": last_flapped,
                "speed": name.get("speed", 0.0) if data["is_up"] else 0.0,
                "mtu": data["mtu"],
                "mac_address": data["mac_address"],
            }
        return result

    def get_interfaces_ip(self) -> Dict[str, models.InterfacesIPDict]:
        """Return the IPv4 and IPv6 addresses configured on the interfaces."""
        result: Dict[str, Dict] = parsers.parse_ip_interfaces(
            self._send_command("show ip interface vrfids 0-512")
        )
        ipv6_output = self._send_command("show ipv6 address interface")
        for intf, data in parsers.parse_ipv6_interfaces(ipv6_output).items():
            result.setdefault(intf, {})["ipv6"] = data["ipv6"]
        return result

    def get_interfaces_counters(self) -> Dict[str, models.InterfaceCounterDict]:
        """Return interface counters.

        ``statistics`` holds the octets, ``statistics verbose`` the unicast /
        multicast / broadcast packets, ``error`` the discards and errors.
        """
        output = "\n".join(
            self._send_command(f"show interfaces gigabitEthernet {table}")
            for table in ("statistics", "statistics verbose", "error")
        )
        return parsers.parse_interface_counters(output)

    def get_lldp_neighbors(self) -> Dict[str, List[models.LLDPNeighborDict]]:
        """Return LLDP neighbors (the system name, or the chassis ID without one)."""
        return {
            port: [
                {
                    "hostname": n["remote_system_name"] or n["remote_chassis_id"],
                    "port": n["remote_port"],
                }
                for n in neighbors
            ]
            for port, neighbors in self.get_lldp_neighbors_detail().items()
        }

    def get_lldp_neighbors_detail(self, interface: str = "") -> models.LLDPNeighborsDetailDict:
        """Return detailed LLDP neighbor information."""
        detail: Dict[str, List] = {}
        if self.transport == "https":
            detail = openapi_parsers.lldp_neighbors_detail(self._api_get("/v0/state/lldp"))
        else:
            command = f"show lldp neighbor port {interface}" if interface else "show lldp neighbor"
            for raw in parsers.parse_lldp_neighbors(self._send_command(command)):
                detail.setdefault(raw["local_port"], []).append(
                    parsers.lldp_neighbor_to_napalm(raw)
                )
        if interface:
            detail = {port: n for port, n in detail.items() if port == interface}
        return detail  # type: ignore[return-value]

    def get_arp_table(self, vrf: str = "") -> List[models.ARPTableDict]:
        """Return the ARP table, optionally for a given VRF."""
        if self.transport == "https":
            # all VRFs in one response; vr_name=... would be case-sensitive
            arp = self._api_get("/v0/state/forwarding/ip-arp")
            if vrf and vrf.lower() not in {t.get("vrName", "").lower() for t in arp}:
                configured = self._api_get("/v0/configuration/vrf")
                if vrf.lower() not in {v.get("name", "").lower() for v in configured}:
                    raise CommandErrorException(f"VRF {vrf!r} does not exist")
            return openapi_parsers.arp_table(arp, vrf)  # type: ignore[return-value]
        command = "show ip arp"
        if vrf:
            command += f" vrf {vrf}"
        output = self._send_command(command)
        if self._has_cli_error(output):
            raise CommandErrorException(f"Command '{command}' failed: {output.strip()}")
        return parsers.parse_arp_table(output)

    def get_mac_address_table(self) -> List[models.MACAdressTable]:
        """Return the MAC address table."""
        if self.transport == "https":
            return openapi_parsers.mac_address_table(  # type: ignore[return-value]
                self._api_get("/v0/state/forwarding/mac-fdb")
            )
        return parsers.parse_mac_address_table(self._send_command("show vlan mac-address-entry"))

    def get_vlans(self) -> Dict[str, models.VlanDict]:
        """Return the configured VLANs and their member ports."""
        if self.transport == "https":
            return openapi_parsers.vlans(  # type: ignore[return-value]
                self._api_get("/v0/configuration/vlan"), self._api_get("/v1/state/vlan")
            )
        names = parsers.parse_vlan_basic(self._send_command("show vlan basic"))
        members = parsers.parse_vlan_members(self._send_command("show vlan members"))
        return {
            vlan_id: {"name": name, "interfaces": members.get(vlan_id, [])}  # type: ignore[misc]
            for vlan_id, name in names.items()
        }

    def get_ntp_servers(self) -> Dict[str, models.NTPServerDict]:
        """Return the configured NTP servers."""
        if self.transport == "https":
            return openapi_parsers.ntp_servers(self._api_get("/v0/configuration/ntp"))  # type: ignore[return-value]
        running = self._send_command("show running-config")
        return {server: {} for server in parsers.parse_ntp_servers(running)}

    def get_snmp_information(self) -> models.SNMPDict:
        """Return SNMP information.

        ``community`` is always empty: Fabric Engine never displays SNMPv1/v2c
        community strings (stored encrypted outside the configuration, masked
        in ``show snmp-server community``). The https transport returns them.
        """
        if self.transport == "https":
            return openapi_parsers.snmp_information(  # type: ignore[return-value]
                self._api_get("/v0/configuration/snmp"), self._api_get("/v0/state/system")
            )
        sys_info = parsers.parse_sys_info(self._send_command("show sys-info"))
        return {
            "chassis_id": sys_info.get("Serial#", ""),
            "contact": sys_info.get("SysContact", ""),
            "location": sys_info.get("SysLocation", ""),
            "community": {},
        }

    def get_users(self) -> Dict[str, models.UsersDict]:
        """Return the user accounts (default and user defined, also disabled ones).

        The access level is mapped to NAPALM's privilege level (``rwa`` 15,
        ``rw`` 14, ``l3`` 4, ``l2`` 3, ``l1`` 2, ``ro`` 1). Only the https
        transport returns ``password`` (the encrypted secret the switch
        stores); ``show cli username`` shows no passwords. SSH keys are not
        reported by either.
        """
        if self.transport == "https":
            return openapi_parsers.users(self._api_get("/v0/configuration/accounts"))  # type: ignore[return-value]
        return parsers.parse_cli_username(self._send_command("show cli username"))  # type: ignore[return-value]

    def get_config(
        self,
        retrieve: str = "all",
        full: bool = False,
        sanitized: bool = False,
        format: str = "text",
    ) -> models.ConfigDict:
        """Return the running and/or startup configuration.

        Fabric Engine has no candidate datastore, so ``candidate`` is always
        an empty string.
        """
        if format != "text":
            raise NotImplementedError("Only 'text' format is supported")
        config: models.ConfigDict = {"running": "", "startup": "", "candidate": ""}
        if retrieve in ("all", "running"):
            command = "show running-config verbose" if full else "show running-config"
            config["running"] = self._clean_config(self._send_command(command))
        if retrieve in ("all", "startup"):
            startup = self._send_command(f"more {self.startup_config_file}")
            # no saved configuration (e.g. a new switch), Fabric Engine 9.4:
            # Can't open "/intflash/config.cfg" 0x2 to read
            if re.match(r"\s*Can't open ", startup):
                startup = ""
            config["startup"] = self._clean_config(startup)
        if sanitized:
            return sanitize_configs(config, SANITIZE_FILTERS)
        return config

    # ------------------------------------------------------------------
    # Configuration management
    # ------------------------------------------------------------------

    @staticmethod
    def _read_candidate(filename: Optional[str], config: Optional[str]) -> str:
        if filename and config:
            raise ValueError("Pass either 'filename' or 'config', not both")
        if filename:
            with open(filename) as handle:
                return handle.read()
        if config is None:
            raise ValueError("Either 'filename' or 'config' must be provided")
        return config

    def load_merge_candidate(
        self, filename: Optional[str] = None, config: Optional[str] = None
    ) -> None:
        """Load configuration lines to be merged on commit.

        With ``config_method="cli"`` the candidate is kept in memory and sent
        line by line on commit; with ``config_method="file"`` it is also copied
        to ``merge_cfg`` on the switch (like the IOS driver).
        """
        try:
            candidate = self._read_candidate(filename, config)
        except (OSError, ValueError) as exc:
            raise MergeConfigException(str(exc)) from exc
        self.config_replace = False
        self.user_changes = []
        self.candidate_config = "\n".join(parsers.config_lines(candidate))
        if self.config_method == "file":
            try:
                self._upload_config(self.candidate_config, self.merge_cfg)
            except CommandErrorException as exc:
                raise MergeConfigException(str(exc)) from exc

    def load_replace_candidate(
        self, filename: Optional[str] = None, config: Optional[str] = None
    ) -> None:
        """Copy a full configuration to ``candidate_cfg`` on the switch.

        ``commit_config`` installs it as the boot configuration and reboots the
        switch (requires ``allow_reboot``), see ``commit_config``.
        """
        try:
            candidate = self._read_candidate(filename, config)
            self.user_changes = []
            self.config_replace = True
            self.candidate_config = candidate.strip()
            self._upload_config(self.candidate_config, self.candidate_cfg)
        except (OSError, ValueError, CommandErrorException) as exc:
            self.discard_config()
            raise ReplaceConfigException(str(exc)) from exc

    def _gen_full_path(self, filename: str) -> str:
        if filename.startswith("/"):
            return filename
        return f"{self.dest_file_system}/{filename}"

    def _upload_config(self, config: str, dest_file: str) -> None:
        """Copy a configuration to the switch with SCP, as a config file."""
        content = config
        if not re.search(r"^\s*conf(?:ig(?:ure)?)?\s+t(?:erminal)?\b", content, re.I | re.M):
            content = "config terminal\n" + content
        if not re.search(r"^\s*end\s*$", content, re.M):
            content += "\nend"
        handle, tmp_file = tempfile.mkstemp(prefix="napalm_fe_", suffix=".cfg")
        try:
            with os.fdopen(handle, "w") as tmp:
                tmp.write(content + "\n")
            with ExtremeVspFileTransfer(
                ssh_conn=self._ssh(),
                source_file=tmp_file,
                dest_file=dest_file,
                file_system=self.dest_file_system,
            ) as transfer:
                if not transfer.verify_space_available():
                    raise CommandErrorException("Insufficient space on the switch")
                transfer.transfer_file()
                if not transfer.verify_file():
                    raise CommandErrorException("File transfer to the switch failed")
        except CommandErrorException:
            raise
        except Exception as exc:
            raise CommandErrorException(f"File transfer to the switch failed: {exc}") from exc
        finally:
            os.remove(tmp_file)

    def compare_config(self) -> str:
        """Return the difference between the candidate and the running config.

        Fabric Engine has no on-box config diff, so the driver compares:

        * merge: only lines that would change something (like the IOS driver's
          incremental diff): ``+line`` is added, ``-line`` removed (``no ...``);
          context lines (``interface ...``) are shown without a prefix
        * replace: ``-line`` / ``+line`` for every line that differs between the
          running configuration and the candidate
        """
        if self.user_changes:
            return self._user_diff()
        if not self.candidate_config:
            return ""
        running = self._send_command("show running-config")
        if self.config_replace:
            return parsers.replace_diff(self._clean_config(running), self.candidate_config)
        return parsers.merge_diff(running, self.candidate_config)

    def commit_config(self, message: str = "", revert_in: Optional[int] = None) -> None:
        """Apply the candidate (like the IOS driver).

        The running configuration is always saved to ``rollback_cfg`` first.

        * merge, ``config_method="cli"``: the lines are sent in configuration
          mode; the first rejected line stops the merge and raises
          ``MergeConfigException`` (lines before it stay applied)
        * merge, ``config_method="file"``: the uploaded file is applied with
          ``source``
        * replace (requires ``allow_reboot``): Fabric Engine cannot replace the
          running configuration. Like ExtremeCloud IQ Site Engine's
          configuration restore, the candidate is copied over the boot
          configuration (``startup_config_file``) and the switch is rebooted.
          The connection is closed; call ``open()`` again once the switch is up.

        A merge is saved with ``save config`` unless ``save_config`` is ``False``.

        ``revert_in`` (seconds, merge only, requires ``allow_reboot``) works like
        ExtremeCloud IQ Site Engine: the current configuration is saved
        (``save config``), a reset is scheduled (``reset -in``, rounded down to
        whole minutes like the IOS driver) and the change is applied without
        saving. If ``confirm_commit()`` is not called in time - e.g. because
        the change made the switch unreachable - the switch reboots into the
        saved configuration.
        """
        if message:
            raise NotImplementedError("Commit message not implemented for this platform")
        if self.user_changes:
            if revert_in is not None:
                raise NotImplementedError("revert_in is not supported for set_users / delete_users")
            self._commit_users()
            return
        if self.pending_revert or (revert_in is not None and self.has_pending_commit()):
            raise CommitConfirmException("A commit is waiting for confirm_commit() or rollback()")
        revert_time = None
        if revert_in is not None:
            if self.config_replace:
                raise NotImplementedError("revert_in is not supported for a replace commit")
            self._check_reboot_allowed("revert_in", CommitConfirmException)
            revert_time = self._revert_time(revert_in)
        if not self.candidate_config:
            exc_class = ReplaceConfigException if self.config_replace else MergeConfigException
            raise exc_class("No candidate configuration loaded")

        if self.config_replace:
            self._check_reboot_allowed("A replace commit")
            logger.info("%s: replacing the configuration (reboot)", self.hostname)
            self._gen_rollback_cfg(ReplaceConfigException)
            self._install_and_reboot(self._gen_full_path(self.candidate_cfg))
            self.candidate_config = None
            self.config_replace = False
            return

        logger.info(
            "%s: merging %d line(s) (config_method=%s)",
            self.hostname,
            len(self.candidate_config.splitlines()),
            self.config_method,
        )
        self._gen_rollback_cfg(MergeConfigException)
        if revert_time:
            # the switch reboots into this saved configuration unless confirmed
            self._copy_run_start(CommitConfirmException)
            self._schedule_reset(revert_time)
        if self.config_method == "file":
            self._commit_from_file()
        else:
            self._commit_from_cli()
        if self.device is not None:
            # a new hostname ("prompt", set_hostname template) changes the SSH
            # prompt; update Netmiko's base prompt like the NX-OS driver
            try:
                self.device.set_base_prompt()
            except AttributeError:
                pass

        if self.save_on_commit and not revert_time:
            self._copy_run_start(CommandErrorException)
        self.candidate_config = None

    # Maximum of "reset -in": 9 days, 23 hours, 59 minutes
    REVERT_MAX_MINUTES = 9 * 1440 + 23 * 60 + 59

    @classmethod
    def _revert_time(cls, revert_in: int) -> str:
        """Seconds -> ``reset -in`` time (``mm``, ``hh:mm`` or ``d:hh:mm``)."""
        minutes = int(revert_in) // 60
        if not 1 <= minutes <= cls.REVERT_MAX_MINUTES:
            raise CommitConfirmException(
                "revert_in is rounded down to whole minutes and must be between "
                f"60 and {cls.REVERT_MAX_MINUTES * 60} seconds"
            )
        days, rest = divmod(minutes, 1440)
        hours, mins = divmod(rest, 60)
        if days:
            return f"{days}:{hours:02d}:{mins:02d}"
        if hours:
            return f"{hours:02d}:{mins:02d}"
        return str(mins)

    def _schedule_reset(self, revert_time: str) -> None:
        """Schedule the revert reboot.

        Over SSH the switch asks ``Are you sure you want to perform a scheduled
        reset? (y/n) ?``; any other answer than ``y`` aborts with ``Scheduled
        reset operation aborted!``. On success the switch prints nothing itself
        (only the log message "Scheduled reset enabled! ..."). The API's CLI
        operation confirms itself. (Its ``reboot/:schedule`` is not used: it
        only accepts 10 to 360 minutes.)
        """
        output = self._confirm(f"reset -in {revert_time}")
        if self._has_cli_error(output) or re.search(r"aborted", output, re.I):
            raise CommitConfirmException(f"Could not schedule the revert reset:\n{output}")
        self.pending_revert = True
        logger.info("%s: revert reset scheduled in %s", self.hostname, revert_time)
        self._wait_for_log(pending=True)

    def has_pending_commit(self) -> bool:
        """Whether a reset is scheduled on the switch (like IOS, asks the switch).

        Fabric Engine has no command that shows a scheduled reset, but logs
        scheduling, cancelling and booting. A reset is pending if the newest
        "Scheduled reset enabled" entry is newer than the newest "Scheduled
        reset canceled" and "System boot" entries (an expired timer reboots
        the switch, which logs "System boot"). So a commit with ``revert_in``
        made by another session is found too.
        """
        scheduled, canceled, booted = (
            parsers.latest_log_time(self._send_command(f"show logging file event-code {code}"))
            for code in (EVENT_RESET_SCHEDULED, EVENT_RESET_CANCELED, EVENT_SYSTEM_BOOT)
        )
        if scheduled is None:
            return False
        return all(scheduled > other for other in (canceled, booted) if other)

    def confirm_commit(self) -> None:
        """Cancel the scheduled revert reset and save the configuration."""
        if not (self.pending_revert or self.has_pending_commit()):
            raise CommitConfirmException("No pending configuration")
        output = self._send_command_timing("reset -cancel")
        self.pending_revert = False
        if re.search(r"No ongoing scheduled reset", output, re.I):
            raise CommitConfirmException(
                "No scheduled reset on the switch any more - it may already have "
                f"reverted the configuration:\n{output.strip()}"
            )
        # Fabric Engine 9.4: "Scheduled reset successfully canceled!"
        if not re.search(r"successfully cancel", output, re.I):
            raise CommitConfirmException(
                f"Could not confirm that the revert reset was cancelled:\n{output.strip()}"
            )
        self._wait_for_log(pending=False)
        self._copy_run_start(CommitConfirmException)

    def _wait_for_log(self, pending: bool) -> None:
        """Wait until ``has_pending_commit()`` shows the reset just (un)scheduled.

        The switch logs "Scheduled reset enabled / canceled" a few seconds
        after the command; without waiting, ``has_pending_commit()`` right
        after a commit or confirm would still report the old state. The
        command itself succeeded, so a timeout is only logged.
        """
        deadline = time.monotonic() + LOG_DELAY
        while self.has_pending_commit() != pending:
            if time.monotonic() > deadline:
                logger.warning("%s: the switch log does not show the change yet", self.hostname)
                return
            time.sleep(1)

    def _gen_rollback_cfg(self, exc_class: type = MergeConfigException) -> None:
        """Save the running config to ``rollback_cfg`` (like the IOS driver)."""
        self._save_config_file(self._gen_full_path(self.rollback_cfg), exc_class)

    @classmethod
    def _save_successful(cls, output: str) -> bool:
        """Check the output of ``save config [file ...]``.

        Success looks like::

            CP-1: Save config to file /intflash/test2.cfg successful.
            WARNING: Choice Primary Node Config file is "/intflash/config.cfg".
        """
        return not cls._has_cli_error(output) and bool(
            re.search(r"Save config to file \S+ successful", output, re.I)
        )

    def _merge_failed(self, output: str) -> MergeConfigException:
        """The error for a failed merge (wording like napalm-dellos10).

        Lines before the rejected one stay applied; Fabric Engine can only
        roll back by rebooting, so this is left to the caller.
        """
        return MergeConfigException(
            "Configuration merge failed; automatic rollback not available, the "
            "switch may be left partly changed (not saved). The configuration "
            f"before the commit is in {self._gen_full_path(self.rollback_cfg)}; "
            "rollback() with optional_args allow_reboot=True restores it (reboot):\n"
            f"{output.strip()}"
        )

    def _commit_from_file(self) -> None:
        output = self._source_file(self._gen_full_path(self.merge_cfg))
        if self._has_cli_error(output):
            raise self._merge_failed(output)

    def _commit_from_cli(self) -> None:
        lines = (self.candidate_config or "").splitlines()
        try:
            output = self._send_config(lines)
        except ConfigInvalidException as exc:
            raise self._merge_failed(str(exc)) from exc
        if self._has_cli_error(output):
            raise self._merge_failed(output)

    def _check_reboot_allowed(self, what: str, exc_class: type = ReplaceConfigException) -> None:
        if not self.allow_reboot:
            raise exc_class(
                f"{what} reboots the switch (Fabric Engine has no configuration "
                "replace); set optional_args allow_reboot=True to allow it"
            )

    def _confirm(self, command: str) -> str:
        """Run a command and answer its (y/n) confirmation prompt.

        e.g. ``copy`` to an existing file asks
        ``File [/intflash/test2.cfg] already existing, overwrite (y/n) ?``
        """
        output = self._send_command_timing(command)
        if re.search(r"\(y/n\)", output):
            output += self._send_command_timing("y")
        return output

    def _file_exists(self, path: str) -> bool:
        entry = parse_dir(self._send_command(f"dir {path.rsplit('/', 1)[0]} -l")).get(path)
        return entry is not None and not entry["is_dir"]

    def _install_and_reboot(self, config_file: str) -> None:
        """Make ``config_file`` the boot configuration and reboot the switch."""
        if not self._file_exists(config_file):
            raise ReplaceConfigException(f"{config_file} does not exist on the switch")
        output = self._confirm(f"copy {config_file} {self.startup_config_file}")
        if self._has_cli_error(output):
            raise ReplaceConfigException(
                f"Could not copy {config_file} to {self.startup_config_file}:\n{output}"
            )
        logger.warning("%s: rebooting to load %s", self.hostname, config_file)
        self._reboot()

    def _reboot(self) -> None:
        try:
            # "-y" skips "Are you sure you want to reset the switch (y/n) ?"
            self._send_command_timing("reset -y")
        except Exception:  # noqa: BLE001 - the session ends when the switch reboots
            pass
        self.close()

    # ------------------------------------------------------------------
    # Users (set_users / delete_users)
    # ------------------------------------------------------------------

    def load_template(
        self,
        template_name: str,
        template_source: Optional[str] = None,
        template_path: Optional[str] = None,
        **template_vars: Any,
    ) -> None:
        """Load a configuration template (NAPALM's ``load_template``).

        ``set_users`` and ``delete_users`` (``users``: NAPALM's ``get_users``
        model) are not configuration lines on Fabric Engine: creating a user
        asks for the password interactively and deleting one asks (y/n). They
        are kept as pending user changes; ``commit_config`` applies them like
        ExtremeCloud IQ Site Engine does - over SSH by answering the prompts,
        over the API through ``configuration/accounts`` (the API's CLI
        operation must not get password prompts: it crashed a 9.4 switch).
        All other templates render to configuration lines (merge candidate).
        """
        if template_name in USER_TEMPLATES and template_source is None and template_path is None:
            self._load_user_changes(template_name, template_vars.get("users") or {})
            return
        super().load_template(template_name, template_source, template_path, **template_vars)

    def _load_user_changes(self, template_name: str, users: Dict[str, Dict]) -> None:
        """Validate ``users`` against the switch and keep them as the candidate.

        ``set_users`` creates users that do not exist yet (level 1 = ro, 14 =
        rw, 15 = rwa; a password is required). For an existing user (also a
        default account) it changes the password; Fabric Engine needs the old
        one for that (``old_password``, not part of NAPALM's model) and
        cannot change the level. ``delete_users`` deletes user defined
        accounts; unknown users are skipped, the default accounts cannot be
        deleted.
        """
        try:
            existing = self.get_users()
            changes: List[tuple] = []
            for name, details in users.items():
                details = details or {}
                if template_name == "delete_users":
                    if name in DEFAULT_ACCOUNTS:
                        raise ValueError(f"{name} is a default account and cannot be deleted")
                    if name in existing:
                        changes.append(("delete", name))
                    continue
                if details.get("sshkeys"):
                    raise ValueError("SSH keys of users cannot be configured on Fabric Engine")
                if name in existing:
                    change = self._user_password_change(name, existing[name], details)
                    if change:
                        changes.append(change)
                    continue
                level = USER_CREATE_LEVELS.get(details.get("level", 1))
                if level is None:
                    raise ValueError(
                        f"{name}: level must be 1 (ro), 14 (rw) or 15 (rwa), "
                        "the levels Fabric Engine can create users with"
                    )
                if not details.get("password"):
                    raise ValueError(f"{name}: a password is required to create a user")
                changes.append(("add", name, level, details["password"]))
        except ValueError as exc:
            raise MergeConfigException(str(exc)) from exc
        self.candidate_config = None
        self.config_replace = False
        self.user_changes = changes

    @staticmethod
    def _user_password_change(name: str, current: Dict, details: Dict) -> Optional[tuple]:
        """The change of an existing user's password, or None if nothing changes."""
        level = {v: k for k, v in parsers.ACCESS_LEVELS.items()}[current["level"]]
        if details.get("level", current["level"]) != current["level"]:
            raise ValueError(f"{name}: Fabric Engine cannot change the level of a user")
        if not details.get("password"):
            return None  # already as requested
        if not details.get("old_password"):
            raise ValueError(
                f"{name} exists: Fabric Engine changes a password only with the old one "
                "(old_password)"
            )
        return ("password", name, level, details["password"], details["old_password"])

    def _user_diff(self) -> str:
        """``compare_config`` for user changes (passwords are not shown)."""
        lines = {
            "add": "+username add {1} level {2} enable",
            "password": "+cli password {1} {level} (new password)",
            "delete": "-username {1}",
        }
        return "\n".join(
            lines[c[0]].format(*c, level=CLI_PASSWORD_LEVELS[c[2]] if len(c) > 2 else "")
            for c in self.user_changes
        )

    def _commit_users(self) -> None:
        """Apply the pending user changes (see ``load_template``)."""
        for change in self.user_changes:
            try:
                if self.transport == "https":
                    self._apply_user_change_api(change)
                else:
                    self._apply_user_change_ssh(change)
            except CommandErrorException as exc:
                raise MergeConfigException(f"{change[0]} user {change[1]} failed: {exc}") from exc
            logger.info("%s: %s user %s", self.hostname, change[0], change[1])
        self.user_changes = []

    def _apply_user_change_api(self, change: tuple) -> None:
        if change[0] == "add":
            _, name, level, password = change
            self._api().post(
                "/v0/configuration/accounts",
                {
                    "username": name,
                    "level": API_ACCOUNT_LEVELS[level],
                    "enabled": True,
                    "secret": {"isEncrypted": False, "value": password},
                },
            )
        elif change[0] == "password":
            _, name, level, password, old_password = change
            self._api().patch(
                f"/v0/configuration/accounts/{name}",
                {
                    "level": API_ACCOUNT_LEVELS[level],
                    "secret": {"isEncrypted": False, "value": password},
                    "oldSecret": {"isEncrypted": False, "value": old_password},
                },
            )
        else:
            self._api().delete(f"/v0/configuration/accounts/{change[1]}")

    def _apply_user_change_ssh(self, change: tuple) -> None:
        """Answer the prompts like ExtremeCloud IQ Site Engine's scripts.

        ``username add <name> level <l> enable`` asks ``Enter password :`` and
        ``Re-enter password :``; ``cli password <name> <level>`` asks ``Enter
        the old password :``, ``Enter the New password :`` and ``Re-enter the
        New password :``; ``no username <name>`` asks ``The specified user
        name will be deleted! Continue (y/n) ?`` (Fabric Engine 9.4). An
        answer is only sent after its prompt.
        """
        device = self._ssh()
        device.config_mode()
        try:
            if change[0] == "add":
                _, name, level, password = change
                output = self._answer_prompts(
                    f"username add {name} level {level} enable",
                    [("Enter password", password), ("Re-enter password", password)],
                )
            elif change[0] == "password":
                _, name, level, password, old_password = change
                output = self._answer_prompts(
                    f"cli password {name} {CLI_PASSWORD_LEVELS[level]}",
                    [
                        ("Enter the old password", old_password),
                        ("Enter the New password", password),
                        ("Re-enter the New password", password),
                    ],
                )
            else:
                output = self._send_command_timing(f"no username {change[1]}")
                if "(y/n)" not in output:
                    raise CommandErrorException(f"unexpected answer: {output.strip()}")
                output = self._send_command_timing("y")
            if self._has_cli_error(output) or re.search(r"aborted|fail", output, re.I):
                raise CommandErrorException(output.replace("*", "").strip())
        finally:
            device.exit_config_mode()

    def _answer_prompts(self, command: str, prompts: List[tuple]) -> str:
        """Send ``command``, then each answer after its prompt (never without it)."""
        output = self._send_command_timing(command)
        for prompt, answer in prompts:
            if prompt.lower() not in output.lower():
                raise CommandErrorException(f"unexpected answer: {output.replace('*', '').strip()}")
            output = self._send_command_timing(answer)
        return output

    def rollback(self) -> None:
        """Restore the configuration saved before the last commit.

        Like ExtremeCloud IQ Site Engine's configuration restore, the saved
        ``rollback_cfg`` is copied over the boot configuration and the switch
        is rebooted (requires ``allow_reboot``). The connection is closed.

        While a commit with ``revert_in`` is pending, the revert happens now
        (``reset -y``): the switch reboots into the configuration saved before
        that commit (like IOS ``configure revert now``).
        """
        self._check_reboot_allowed("rollback()")
        if self.pending_revert or self.has_pending_commit():
            logger.warning("%s: reverting the pending commit now (reboot)", self.hostname)
            self.pending_revert = False
            self._reboot()
            return
        self._install_and_reboot(self._gen_full_path(self.rollback_cfg))

    def discard_config(self) -> None:
        """Discard the pending candidate.

        Like NAPALM's NX-OS drivers, a replace candidate uploaded to the switch
        (``candidate_cfg``) is deleted as well.
        """
        if self.config_replace and self.candidate_config and (self.device or self.api):
            self._delete_file(self._gen_full_path(self.candidate_cfg))
        self.candidate_config = None
        self.config_replace = False
        self.user_changes = []

    def _delete_file(self, path: str) -> None:
        """Delete a file on the switch (answers ``Are you sure (y/n) ?``).

        Best effort: a failure is logged, not raised.
        """
        try:
            output = self._confirm(f"delete {path}")
        except Exception as exc:  # noqa: BLE001 - cleanup must not fail
            logger.warning("%s: could not delete %s: %s", self.hostname, path, exc)
            return
        if self._has_cli_error(output) or re.search(r"can't|cannot|fail", output, re.I):
            logger.warning("%s: could not delete %s: %s", self.hostname, path, output.strip())
