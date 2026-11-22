"""NAPALM driver for Extreme Networks Fabric Engine (formerly VOSS / VSP).

Like NAPALM's IOS driver, the driver talks to the switch over SSH (Netmiko,
device type ``extreme_vsp``) and parses the CLI output; it works on every
release.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Union

from napalm.base import NetworkDriver, models
from napalm.base.exceptions import (
    CommandErrorException,
    ConnectionClosedException,
    MergeConfigException,
)
from napalm.base.helpers import sanitize_configs
from napalm.base.netmiko_helpers import netmiko_args
from netmiko.exceptions import ConfigInvalidException

from napalm_fe import parsers

logger = logging.getLogger(__name__)

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

# Output that shows that the CLI rejected a command. Fabric Engine 9.4 also
# reports errors as e.g. "Error: The community entry already exists." (no
# "%"), sometimes after a BEL ("\x07Error: The specified VLAN ID is invalid or
# already in use"). Also passed to Netmiko's
# send_config_set(error_pattern=...), which applies it with re.M as well.
CLI_ERROR_RE = re.compile(
    r"% (?:Invalid input|Incomplete command|Ambiguous command|Unrecognized command|Cannot |Error)"
    r"|^[\s\x07]*Error\s*:",
    re.M,
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

        * ``save_config`` (bool, default ``True``): run ``save config`` after
          a successful ``commit_config``.
        * ``startup_config_file`` (str, default ``/intflash/config.cfg``):
          the file returned as the ``startup`` configuration by ``get_config``.
        """
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        optional_args = optional_args or {}

        self.save_on_commit = bool(optional_args.get("save_config", True))
        self.startup_config_file = optional_args.get("startup_config_file", "/intflash/config.cfg")
        self.force_no_enable = bool(optional_args.get("force_no_enable", False))
        self.netmiko_optional_args = netmiko_args(optional_args)
        self.device: Any = None
        self.candidate_config: Optional[str] = None
        self.profile = [self.platform]

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the SSH connection to the device."""
        self.device = self._netmiko_open(
            NETMIKO_DEVICE_TYPE, netmiko_optional_args=self.netmiko_optional_args
        )

    def close(self) -> None:
        """Close the connection to the device."""
        if self.device is not None:
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
        except (OSError, EOFError, AttributeError):
            return {"is_alive": False}

    # ------------------------------------------------------------------
    # Transport primitives (like NAPALM's NX-OS drivers): everything that
    # talks to Netmiko is here.
    # ------------------------------------------------------------------

    def _ssh(self) -> Any:
        """The Netmiko session."""
        if self.device is None:
            raise ConnectionClosedException("Not connected to the device, call open() first")
        return self.device

    def _send_command(self, command: str, **kwargs: Any) -> str:
        """Send a command (Netmiko ``send_command``, ``kwargs`` are passed on)
        and return its post-processed output."""
        device = self._ssh()
        # Large tables (running-config, FDB on a chassis) can take longer than
        # Netmiko's 10 second default; use the driver timeout instead.
        kwargs.setdefault("read_timeout", self.timeout)
        logger.debug("%s: sending %r", self.hostname, command)
        try:
            output = str(device.send_command(command, **kwargs))
        except (OSError, EOFError) as exc:
            raise ConnectionClosedException(str(exc)) from exc
        return self._send_command_postprocess(
            output, remove_log_messages=not LOG_DISPLAY_COMMAND_RE.match(command)
        )

    def _send_config(self, lines: List[str]) -> str:
        """Send lines in configuration mode; stop at the first rejected line.

        Raises ``ConfigInvalidException`` (Netmiko) for a rejected line.
        """
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

    def _copy_run_start(self, exc_class: type = CommandErrorException) -> None:
        """Save the running configuration (``save config``)."""
        output = str(self._ssh().save_config())
        if not self._save_successful(output):
            raise exc_class(f"Saving the configuration failed:\n{output}")

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
        sys_info = parsers.parse_sys_info(self._send_command("show sys-info"))
        # only the port list is needed: the "name" table, not all 13 tables
        interfaces = parsers.parse_interfaces_name(
            self._send_command("show interfaces gigabitEthernet name")
        )

        hostname = sys_info.get("SysName", "")
        model = sys_info.get("ModelName") or sys_info.get("Chassis", "")
        domain = parsers.parse_domain_name(self._send_command("show ip dns"))
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
        command = f"show lldp neighbor port {interface}" if interface else "show lldp neighbor"
        for raw in parsers.parse_lldp_neighbors(self._send_command(command)):
            detail.setdefault(raw["local_port"], []).append(parsers.lldp_neighbor_to_napalm(raw))
        if interface:
            detail = {port: n for port, n in detail.items() if port == interface}
        return detail  # type: ignore[return-value]

    def get_arp_table(self, vrf: str = "") -> List[models.ARPTableDict]:
        """Return the ARP table, optionally for a given VRF."""
        command = "show ip arp"
        if vrf:
            command += f" vrf {vrf}"
        output = self._send_command(command)
        if self._has_cli_error(output):
            raise CommandErrorException(f"Command '{command}' failed: {output.strip()}")
        return parsers.parse_arp_table(output)

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
        """Return the NTP servers the switch uses.

        ``show ntp server`` also lists servers that are not in the running
        configuration, such as the one the cloud agent sets.
        """
        output = self._send_command("show ntp server")
        return {server: {} for server in parsers.parse_ntp_servers(output)}

    def get_snmp_information(self) -> models.SNMPDict:
        """Return SNMP information.

        ``community`` is always empty: Fabric Engine never displays SNMPv1/v2c
        community strings (stored encrypted outside the configuration, masked
        in ``show snmp-server community``).
        """
        sys_info = parsers.parse_sys_info(self._send_command("show sys-info"))
        return {
            "chassis_id": sys_info.get("Serial#", ""),
            "contact": sys_info.get("SysContact", ""),
            "location": sys_info.get("SysLocation", ""),
            "community": {},
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
        """Load configuration lines to be merged on commit (kept in memory)."""
        try:
            candidate = self._read_candidate(filename, config)
        except (OSError, ValueError) as exc:
            raise MergeConfigException(str(exc)) from exc
        self.candidate_config = "\n".join(parsers.config_lines(candidate))

    def compare_config(self) -> str:
        """Return the difference between the candidate and the running config.

        Fabric Engine has no on-box config diff, so the driver compares: only
        lines that would change something are shown (like the IOS driver's
        incremental diff): ``+line`` is added, ``-line`` removed (``no ...``);
        context lines (``interface ...``) are shown without a prefix.
        """
        if not self.candidate_config:
            return ""
        running = self._send_command("show running-config")
        return parsers.merge_diff(running, self.candidate_config)

    def commit_config(self, message: str = "", revert_in: Optional[int] = None) -> None:
        """Apply the merge candidate (like the IOS driver).

        The lines are sent in configuration mode; the first rejected line stops
        the merge and raises ``MergeConfigException`` (lines before it stay
        applied).

        A merge is saved with ``save config`` unless ``save_config`` is ``False``.
        ``revert_in`` (commit confirm) is not supported.
        """
        if message:
            raise NotImplementedError("Commit message not implemented for this platform")
        if revert_in is not None:
            raise NotImplementedError("Commit confirm has not been implemented on this platform.")
        if not self.candidate_config:
            raise MergeConfigException("No candidate configuration loaded")

        logger.info(
            "%s: merging %d line(s)", self.hostname, len(self.candidate_config.splitlines())
        )
        self._commit_from_cli()
        if self.device is not None:
            # a new hostname ("prompt", set_hostname template) changes the SSH
            # prompt; update Netmiko's base prompt like the NX-OS driver
            try:
                self.device.set_base_prompt()
            except AttributeError:
                pass

        if self.save_on_commit:
            self._copy_run_start(CommandErrorException)
        self.candidate_config = None

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
            f"switch may be left partly changed (not saved):\n{output.strip()}"
        )

    def _commit_from_cli(self) -> None:
        lines = (self.candidate_config or "").splitlines()
        try:
            output = self._send_config(lines)
        except ConfigInvalidException as exc:
            raise self._merge_failed(str(exc)) from exc
        if self._has_cli_error(output):
            raise self._merge_failed(output)

    def discard_config(self) -> None:
        """Discard the pending candidate."""
        self.candidate_config = None
