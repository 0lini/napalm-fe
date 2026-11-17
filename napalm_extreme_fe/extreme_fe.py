"""NAPALM driver for Extreme Networks Fabric Engine (formerly VOSS / VSP).

The driver talks to the device over SSH using Netmiko's ``extreme_vsp``
device type and parses the CLI output.
"""

import re
import socket
from typing import Any, Dict, List, Optional, Union

from napalm.base import NetworkDriver, models
from napalm.base.exceptions import (
    CommandErrorException,
    CommitError,
    ConnectionClosedException,
    MergeConfigException,
    ReplaceConfigException,
)
from napalm.base.helpers import sanitize_configs
from napalm.base.netmiko_helpers import netmiko_args

from napalm_extreme_fe import parsers

NETMIKO_DEVICE_TYPE = "extreme_vsp"

# Strings that indicate that the CLI rejected a command
CLI_ERROR_PATTERNS = (
    r"% Invalid input",
    r"% Incomplete command",
    r"% Ambiguous command",
    r"% Unrecognized command",
    r"% Cannot ",
    r"^\s*Error\s*:",
    r"% Error",
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
            return {"is_alive": bool(self.device.is_alive())}
        except (socket.error, EOFError, OSError):
            return {"is_alive": False}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_command(self, command: str, **kwargs: Any) -> str:
        if self.device is None:
            raise ConnectionClosedException("Not connected to the device, call open() first")
        try:
            output = self.device.send_command(command, **kwargs)
        except (socket.error, EOFError) as exc:
            raise ConnectionClosedException(str(exc)) from exc
        return output if isinstance(output, str) else str(output)

    @staticmethod
    def _strip_banner(output: str) -> str:
        """Remove the "Command Execution Time" banner and the
        "Preparing to Display Configuration..." line from config output."""
        output = re.sub(
            r"\A\s*\*+[ \t]*\n[^\n]*Command Execution Time:[^\n]*\n\*+[ \t]*\n", "", output
        )
        return re.sub(r"\A\s*Preparing to Display Configuration\.*[ \t]*\n", "", output)

    @staticmethod
    def _has_cli_error(output: str) -> bool:
        return any(re.search(p, output, re.MULTILINE) for p in CLI_ERROR_PATTERNS)

    # ------------------------------------------------------------------
    # CLI
    # ------------------------------------------------------------------

    def cli(
        self, commands: List[str], encoding: str = "text"
    ) -> Dict[str, Union[str, Dict[str, Any]]]:
        """Execute a list of commands and return the raw output."""
        if encoding != "text":
            raise NotImplementedError(f"{encoding} is not a supported encoding")
        if not isinstance(commands, list):
            raise TypeError("Please enter a valid list of commands!")
        result: Dict[str, Union[str, Dict[str, Any]]] = {}
        for command in commands:
            output = self._send_command(command)
            if self._has_cli_error(output):
                raise CommandErrorException(f"Command '{command}' failed: {output.strip()}")
            result[command] = output
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
        return {
            "hostname": hostname,
            "fqdn": hostname,
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
            "cpu": parsers.parse_khi_cpu(cpu_output),  # type: ignore[typeddict-item]
            "memory": parsers.parse_khi_memory(memory_output),  # type: ignore[typeddict-item]
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
        return result  # type: ignore[return-value]

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
        return parsers.parse_interface_counters(output)  # type: ignore[return-value]

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
            result.setdefault(raw["local_port"], []).append(
                parsers.lldp_neighbor_to_napalm(raw)  # type: ignore[arg-type]
            )
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
        return parsers.parse_mac_address_table(  # type: ignore[return-value]
            self._send_command("show vlan mac-address-entry")
        )

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
            running = self._send_command(command)
            config["running"] = self._strip_banner(running)
        if retrieve in ("all", "startup"):
            config["startup"] = self._strip_banner(
                self._send_command(f"more {self.startup_config_file}")
            )
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
        """Store configuration lines to be merged on commit.

        Fabric Engine does not provide a candidate configuration datastore, so
        the candidate is kept in memory and sent line by line on commit.
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
        self.candidate_config = "\n".join(lines)

    def load_replace_candidate(
        self, filename: Optional[str] = None, config: Optional[str] = None
    ) -> None:
        """Configuration replace is not supported by Fabric Engine."""
        raise ReplaceConfigException(
            "Fabric Engine does not support an atomic configuration replace; "
            "use load_merge_candidate instead."
        )

    def compare_config(self) -> str:
        """Return the lines that will be sent to the device on commit.

        Without a candidate datastore on the device there is no real diff, so
        (like other CLI based NAPALM drivers in merge mode) the pending
        candidate is returned, prefixed with ``+``.
        """
        if not self.candidate_config:
            return ""
        return "\n".join(f"+{line}" for line in self.candidate_config.splitlines())

    def commit_config(self, message: str = "", revert_in: Optional[int] = None) -> None:
        """Push the merge candidate to the device."""
        if message:
            raise NotImplementedError("Commit messages are not supported by Fabric Engine")
        if revert_in is not None:
            raise NotImplementedError("Commit confirm is not supported by Fabric Engine")
        if not self.candidate_config:
            raise MergeConfigException("No candidate configuration loaded")

        lines = self.candidate_config.splitlines()
        output = self.device.send_config_set(lines, cmd_verify=False)
        if self._has_cli_error(output):
            # Leave config mode cleanly before raising
            try:
                self.device.exit_config_mode()
            except Exception:  # pragma: no cover - best effort
                pass
            raise CommitError(f"Error while applying configuration:\n{output}")

        if self.save_on_commit:
            save_output = self.device.save_config()
            if self._has_cli_error(save_output):
                raise CommitError(f"Error while saving configuration:\n{save_output}")
        self.candidate_config = None

    def discard_config(self) -> None:
        """Discard the pending candidate."""
        self.candidate_config = None
