"""NAPALM driver for Extreme Networks Fabric Engine (formerly VOSS / VSP).

The driver talks to the device over SSH using Netmiko's ``extreme_vsp``
device type and parses the CLI output.
"""

import logging
import os
import re
import socket
import tempfile
from typing import Any, Dict, List, Optional, Union

from napalm.base import NetworkDriver, models
from napalm.base.exceptions import (
    CommandErrorException,
    CommitConfirmException,
    ConnectionClosedException,
    MergeConfigException,
    ReplaceConfigException,
)
from napalm.base.helpers import sanitize_configs
from napalm.base.netmiko_helpers import netmiko_args
from netmiko.exceptions import ConfigInvalidException
from netmiko.ssh_dispatcher import CLASS_MAPPER

from napalm_extreme_fe import parsers
from napalm_extreme_fe.file_transfer import ExtremeFEFileTransfer, parse_dir

logger = logging.getLogger(__name__)

# Extreme renamed VOSS (VSP Operating System Software) to Fabric Engine;
# Netmiko still calls it "extreme_vsp". Use the newest name Netmiko provides.
SUPPORTED_TRANSPORTS = ("ssh",)

NETMIKO_DEVICE_TYPES = ("extreme_fe", "extreme_voss", "extreme_vsp")
NETMIKO_DEVICE_TYPE = next(t for t in NETMIKO_DEVICE_TYPES if t in CLASS_MAPPER)

# Strings that indicate that the CLI rejected a command
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

# The same, in the older format (seen on the serial console while the switch
# shuts down; probably never in SSH sessions - removed in case it appears):
#   CP1  [11/17/26 16:41:43.000] LifeCycle: INFO: Stopping all  processes
LEGACY_LOG_MESSAGE_RE = re.compile(
    r"(?:^\S+:\d+(?:\([^)\s]*\))?[>#])?"
    r"(?:CP|IO)\d+\s+\[\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\][^\n]*\n?",
    re.M,
)

# Countdown of a scheduled reset ("reset -in"), seen on the serial console
# only (not in SSH sessions) - removed in case it appears:
#           20 seconds remaining until unit resets.
RESET_COUNTDOWN_RE = re.compile(r"^[ \t]*\d+ seconds? remaining until unit resets\.[ \t]*\n?", re.M)

# Commands that display the log: their output consists of log messages, so the
# filter above must not be applied to them.
LOG_DISPLAY_COMMAND_RE = re.compile(r"^\s*show\s+log", re.I)

# Log event codes (Fabric Engine 9.4) used to find a scheduled reset
EVENT_RESET_SCHEDULED = "0x0003068e"  # Scheduled reset enabled! Unit will reset in ...
EVENT_RESET_CANCELED = "0x0003068f"  # Scheduled reset canceled!
EVENT_SYSTEM_BOOT = "0x00034594"  # System boot

CLI_ERROR_PATTERNS = (
    r"% Invalid input",
    r"% Incomplete command",
    r"% Ambiguous command",
    r"% Unrecognized command",
    r"% Cannot ",
    r"^\s*Error\s*:",
    r"% Error",
)

# Same, as a single pattern for Netmiko's send_config_set(error_pattern=...)
# Fabric Engine 9.4 also reports errors as e.g. "Error: The community entry
# already exists." (no "%"); Netmiko applies the pattern with re.M.
CONFIG_ERROR_PATTERN = (
    r"% (?:Invalid input|Incomplete command|Ambiguous command|Unrecognized)|% Error"
    r"|^\s*Error\s*:"
)

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


class ExtremeFEDriver(NetworkDriver):
    """NAPALM driver for Extreme Networks Fabric Engine."""

    platform = "extreme_fe"

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
          switch, like NAPALM's EOS driver. Only ``"ssh"`` (CLI via Netmiko) is
          available; an ``"openapi"`` transport is planned (see AGENTS.md).
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
            raise ValueError(
                f"transport {self.transport!r} is not supported, use one of "
                f"{', '.join(SUPPORTED_TRANSPORTS)} (an 'openapi' transport is planned)"
            )
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

        self.device: Any = None
        self.candidate_config: Optional[str] = None
        self.config_replace = False
        self.pending_revert = False
        self.profile = [self.platform]

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open a connection to the device."""
        self.device = self._netmiko_open(
            NETMIKO_DEVICE_TYPE, netmiko_optional_args=self.netmiko_optional_args
        )

    def close(self) -> None:
        """Close the connection to the device."""
        self._netmiko_close()
        self.device = None

    def is_alive(self) -> models.AliveDict:
        """Return whether the SSH connection is still usable."""
        if self.device is None:
            return {"is_alive": False}
        try:
            # Send an ASCII NUL to keep the session alive, then ask the transport
            self.device.write_channel(chr(0))
            return {"is_alive": bool(self.device.remote_conn.transport.is_active())}
        except (socket.error, EOFError, OSError, AttributeError):
            return {"is_alive": False}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_command(self, command: Union[str, List[str]], **kwargs: Any) -> str:
        """Send a command and return its post-processed output.

        If ``command`` is a list, the commands are tried in order until one is
        accepted by the CLI (useful when the syntax differs between releases).
        """
        if self.device is None:
            raise ConnectionClosedException("Not connected to the device, call open() first")
        commands = command if isinstance(command, list) else [command]
        # Large tables (running-config, FDB on a chassis) can take longer than
        # Netmiko's 10 second default; use the driver timeout instead.
        kwargs.setdefault("read_timeout", self.timeout)
        output = ""
        try:
            for cmd in commands:
                logger.debug("%s: sending %r", self.hostname, cmd)
                output = self.device.send_command(cmd, **kwargs)
                output = output if isinstance(output, str) else str(output)
                if not self._has_cli_error(output):
                    break
        except (socket.error, EOFError) as exc:
            raise ConnectionClosedException(str(exc)) from exc
        return self._send_command_postprocess(
            output, remove_log_messages=not LOG_DISPLAY_COMMAND_RE.match(cmd)
        )

    # Transport primitives (like NAPALM's NX-OS drivers): everything that talks
    # to Netmiko directly is here, so that another transport only has to
    # implement these.

    def _send_command_timing(self, command: str) -> str:
        """Send a command that prompts or has no fixed end (copy, reset, y)."""
        return str(self.device.send_command_timing(command, read_timeout=self.timeout))

    def _send_config(self, lines: List[str]) -> str:
        """Send lines in configuration mode; stop at the first rejected line.

        Raises ``ConfigInvalidException`` (Netmiko) for a rejected line.
        """
        try:
            return str(
                self.device.send_config_set(
                    lines, cmd_verify=False, error_pattern=CONFIG_ERROR_PATTERN
                )
            )
        except ConfigInvalidException:
            try:
                self.device.exit_config_mode()
            except Exception:  # pragma: no cover - best effort
                pass
            raise

    def _copy_run_start(self, exc_class: type = CommandErrorException) -> None:
        """Save the running configuration (``save config``)."""
        output = str(self.device.save_config())
        if not self._save_successful(output):
            raise exc_class(f"Saving the configuration failed:\n{output}")

    @staticmethod
    def _send_command_postprocess(output: str, remove_log_messages: bool = True) -> str:
        """Remove CLI noise from command output (like the IOS driver).

        Removes log messages printed into the session ("logging screen") -
        except for commands that display the log ("show logging ...") - the
        countdown of a scheduled reset and the "Please widen the terminal"
        hint. The "Command Execution Time"
        banner is kept: it is used to compute ``last_flapped``.
        """
        if remove_log_messages:
            output = LOG_MESSAGE_RE.sub("", output)
            output = LEGACY_LOG_MESSAGE_RE.sub("", output)
        output = RESET_COUNTDOWN_RE.sub("", output)
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
        return any(re.search(p, output, re.MULTILINE) for p in CLI_ERROR_PATTERNS)

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
        sys_info = parsers.parse_sys_info(self._send_command("show sys-info"))
        interfaces = parsers.parse_interfaces(self._send_command("show interfaces gigabitEthernet"))

        hostname = sys_info.get("SysName", "")
        model = sys_info.get("ModelName") or sys_info.get("Chassis", "")
        domain = parsers.parse_domain_name(
            self._send_command(["show running-config | include domain-name", "show running-config"])
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
        # Recent releases include the "Port Name" table in the plain command
        if parsers.has_table(output, "Port Name"):
            names = parsers.parse_interfaces_name(output)
        else:
            names = parsers.parse_interfaces_name(
                self._send_command("show interfaces gigabitEthernet name")
            )
        now = parsers.command_execution_time(output)
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
        if not self._has_cli_error(ipv6_output):
            for intf, data in parsers.parse_ipv6_interfaces(ipv6_output).items():
                result.setdefault(intf, {}).setdefault("ipv6", {}).update(data["ipv6"])
        return result

    def get_interfaces_counters(self) -> Dict[str, models.InterfaceCounterDict]:
        """Return interface counters.

        ``statistics verbose`` holds the unicast/multicast/broadcast counters,
        ``error`` the discards and errors. Octets are only in the plain
        ``statistics`` output on 9.x, which is fetched if they are missing.
        """
        outputs = []
        for command in (
            "show interfaces gigabitEthernet statistics verbose",
            "show interfaces gigabitEthernet error",
        ):
            output = self._send_command(command)
            if not self._has_cli_error(output):
                outputs.append(output)
        if not any(
            c["rx_octets"] >= 0
            for c in parsers.parse_interface_counters("\n".join(outputs)).values()
        ):
            outputs.append(self._send_command("show interfaces gigabitEthernet statistics"))
        output = "\n".join(outputs)
        return parsers.parse_interface_counters(output)

    def get_lldp_neighbors(self) -> Dict[str, List[models.LLDPNeighborDict]]:
        """Return LLDP neighbors."""
        result: Dict[str, List[models.LLDPNeighborDict]] = {}
        for raw in parsers.parse_lldp_neighbors(self._send_command("show lldp neighbor")):
            detail = parsers.lldp_neighbor_to_napalm(raw)
            result.setdefault(raw["local_port"], []).append(
                {
                    "hostname": detail["remote_system_name"] or detail["remote_chassis_id"],
                    "port": detail["remote_port"],
                }
            )
        return result

    def get_lldp_neighbors_detail(self, interface: str = "") -> models.LLDPNeighborsDetailDict:
        """Return detailed LLDP neighbor information."""
        command = "show lldp neighbor"
        if interface:
            command += f" port {interface}"
        result: models.LLDPNeighborsDetailDict = {}
        for raw in parsers.parse_lldp_neighbors(self._send_command(command)):
            if interface and raw["local_port"] != interface:
                continue
            result.setdefault(raw["local_port"], []).append(parsers.lldp_neighbor_to_napalm(raw))
        return result

    def get_arp_table(self, vrf: str = "") -> List[models.ARPTableDict]:
        """Return the ARP table, optionally for a given VRF."""
        command = "show ip arp"
        if vrf:
            command += f" vrf {vrf}"
        output = self._send_command(command)
        if self._has_cli_error(output):
            raise CommandErrorException(f"Command '{command}' failed: {output.strip()}")
        return [
            {"interface": e["interface"], "mac": e["mac"], "ip": e["ip"], "age": e["age"]}
            for e in parsers.parse_arp_table(output)
        ]

    def get_mac_address_table(self) -> List[models.MACAdressTable]:
        """Return the MAC address table."""
        return parsers.parse_mac_address_table(self._send_command("show vlan mac-address-entry"))

    def get_vlans(self) -> Dict[str, models.VlanDict]:
        """Return the configured VLANs and their member ports."""
        names = parsers.parse_vlan_basic(self._send_command("show vlan basic"))
        members = parsers.parse_vlan_members(self._send_command("show vlan members"))
        return {
            vlan_id: {"name": name, "interfaces": members.get(vlan_id, [])}  # type: ignore[misc]
            for vlan_id, name in names.items()
        }

    def get_ntp_servers(self) -> Dict[str, models.NTPServerDict]:
        """Return the configured NTP servers."""
        running = self._send_command("show running-config")
        return {server: {} for server in parsers.parse_ntp_servers(running)}

    def get_snmp_information(self) -> models.SNMPDict:
        """Return SNMP information."""
        sys_info = parsers.parse_sys_info(self._send_command("show sys-info"))
        running = self._send_command("show running-config")
        return {
            "chassis_id": sys_info.get("Serial#", ""),
            "contact": sys_info.get("SysContact", ""),
            "location": sys_info.get("SysLocation", ""),
            "community": parsers.parse_snmp_communities(running),
        }

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
        lines = [
            line.rstrip()
            for line in candidate.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.config_replace = False
        self.candidate_config = "\n".join(lines)
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
            with ExtremeFEFileTransfer(
                ssh_conn=self.device,
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
        if not self.candidate_config:
            return ""
        running = self._send_command("show running-config")
        if self.config_replace:
            return parsers.replace_diff(running, self.candidate_config)
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
        if self.pending_revert or (revert_in is not None and self._reset_scheduled_on_switch()):
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

        The switch asks ``Are you sure you want to perform a scheduled reset?
        (y/n) ?``; any other answer than ``y`` aborts with ``Scheduled reset
        operation aborted!``. On success the switch prints nothing itself
        (only the log message "Scheduled reset enabled! ...").
        """
        output = self._confirm(f"reset -in {revert_time}")
        if self._has_cli_error(output) or re.search(r"aborted", output, re.I):
            raise CommitConfirmException(f"Could not schedule the revert reset:\n{output}")
        self.pending_revert = True
        logger.info("%s: revert reset scheduled in %s", self.hostname, revert_time)

    def _reset_scheduled_on_switch(self) -> bool:
        """Read the switch's log to find out whether a reset is scheduled.

        Fabric Engine has no command that shows a scheduled reset, but logs
        scheduling, cancelling and booting. A reset is pending if the newest
        "Scheduled reset enabled" entry is newer than the newest "Scheduled
        reset canceled" and "System boot" entries (an expired timer reboots
        the switch, which logs "System boot").
        """
        latest = {}
        for code in (EVENT_RESET_SCHEDULED, EVENT_RESET_CANCELED, EVENT_SYSTEM_BOOT):
            latest[code] = parsers.latest_log_time(
                self._send_command(f"show logging file event-code {code}")
            )
        scheduled = latest[EVENT_RESET_SCHEDULED]
        if scheduled is None:
            return False
        others = [t for c, t in latest.items() if c != EVENT_RESET_SCHEDULED and t]
        return all(scheduled > other for other in others)

    def has_pending_commit(self) -> bool:
        """Whether a reset is scheduled on the switch (like IOS, asks the switch).

        Reads the switch's log (see ``_reset_scheduled_on_switch``), so a
        commit with ``revert_in`` made by another session is found too.
        """
        return self._reset_scheduled_on_switch()

    def confirm_commit(self) -> None:
        """Cancel the scheduled revert reset and save the configuration."""
        if not (self.pending_revert or self._reset_scheduled_on_switch()):
            raise CommitConfirmException("No pending configuration")
        output = self._send_command_timing("reset -cancel")
        output = LOG_MESSAGE_RE.sub("", output)
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
        self._copy_run_start(CommitConfirmException)

    def _gen_rollback_cfg(self, exc_class: type = MergeConfigException) -> None:
        """Save the running config to ``rollback_cfg`` (like the IOS driver)."""
        cmd = f"save config file {self._gen_full_path(self.rollback_cfg)}"
        output = self._send_command(cmd)
        if not self._save_successful(output):
            raise exc_class(f"Could not save the rollback configuration:\n{output}")

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
        cmd = f"source {self._gen_full_path(self.merge_cfg)}"
        output = self._send_command(cmd, read_timeout=max(self.timeout, 120))
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
        # a log message may arrive after the prompt: remove it before looking
        output = LOG_MESSAGE_RE.sub("", self._send_command_timing(command))
        if re.search(r"\(y/n\)|\[y/n\]|y/n\s*\??\s*$", output.rstrip(), re.I):
            answer = self._send_command_timing("y")
            output += LOG_MESSAGE_RE.sub("", answer)
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
        if self.pending_revert or self._reset_scheduled_on_switch():
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
        if self.config_replace and self.candidate_config and self.device is not None:
            self._delete_file(self._gen_full_path(self.candidate_cfg))
        self.candidate_config = None
        self.config_replace = False

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
