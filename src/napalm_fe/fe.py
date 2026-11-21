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

# NAPALM privilege levels the switch can create users with (username add
# accepts only ro, rw and rwa; Fabric Engine 9.4)
USER_CREATE_LEVELS = {1: "ro", 14: "rw", 15: "rwa"}
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
        # pending set_users / delete_users: ("add", name, level, password),
        # ("password", name, level, password, old_password) or ("delete", name)
        self.user_changes: List[tuple] = []
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

    def _send_command_timing(self, command: str) -> str:
        """Send a command that prompts or has no fixed end (copy, delete, reset, y).

        Log messages printed into the session (they may follow a prompt) are
        removed.
        """
        output = str(self._ssh().send_command_timing(command, read_timeout=self.timeout))
        return LOG_MESSAGE_RE.sub("", output)

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
        """Return the configured NTP servers."""
        running = self._send_command("show running-config")
        return {server: {} for server in parsers.parse_ntp_servers(running)}

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

    def get_users(self) -> Dict[str, models.UsersDict]:
        """Return the user accounts (default and user defined, also disabled ones).

        ``show cli username``; the access level is mapped to NAPALM's
        privilege level (``rwa`` 15, ``rw`` 14, ``l3`` 4, ``l2`` 3, ``l1`` 2,
        ``ro`` 1). The CLI shows no passwords and no SSH keys.
        """
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
        """Load configuration lines to be merged on commit (kept in memory)."""
        try:
            candidate = self._read_candidate(filename, config)
        except (OSError, ValueError) as exc:
            raise MergeConfigException(str(exc)) from exc
        self.user_changes = []
        self.candidate_config = "\n".join(parsers.config_lines(candidate))

    def compare_config(self) -> str:
        """Return the difference between the candidate and the running config.

        Fabric Engine has no on-box config diff, so the driver compares: only
        lines that would change something are shown (like the IOS driver's
        incremental diff): ``+line`` is added, ``-line`` removed (``no ...``);
        context lines (``interface ...``) are shown without a prefix.
        """
        if self.user_changes:
            return self._user_diff()
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
        if self.user_changes:
            if revert_in is not None:
                raise NotImplementedError("revert_in is not supported for set_users / delete_users")
            self._commit_users()
            return
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
        ExtremeCloud IQ Site Engine does, by answering the prompts.
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
                self._apply_user_change_ssh(change)
            except CommandErrorException as exc:
                raise MergeConfigException(f"{change[0]} user {change[1]} failed: {exc}") from exc
            logger.info("%s: %s user %s", self.hostname, change[0], change[1])
        self.user_changes = []

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

    def discard_config(self) -> None:
        """Discard the pending candidate (also pending user changes)."""
        self.candidate_config = None
        self.user_changes = []
