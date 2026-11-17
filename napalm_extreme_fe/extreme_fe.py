"""NAPALM driver for Extreme Networks Fabric Engine (formerly VOSS / VSP).

The driver talks to the device over SSH using Netmiko's ``extreme_vsp``
device type and parses the CLI output.
"""

import os
import re
import socket
import tempfile
from typing import Any, Dict, List, Optional, Union

from napalm.base import NetworkDriver, models
from napalm.base.exceptions import (
    CommandErrorException,
    ConnectionClosedException,
    MergeConfigException,
    ReplaceConfigException,
)
from napalm.base.helpers import sanitize_configs
from napalm.base.netmiko_helpers import netmiko_args
from netmiko.exceptions import ConfigInvalidException
from netmiko.ssh_dispatcher import CLASS_MAPPER

from napalm_extreme_fe import parsers
from napalm_extreme_fe.file_transfer import ExtremeFEFileTransfer

# Extreme renamed VOSS (VSP Operating System Software) to Fabric Engine;
# Netmiko still calls it "extreme_vsp". Use the newest name Netmiko provides.
NETMIKO_DEVICE_TYPES = ("extreme_fe", "extreme_voss", "extreme_vsp")
NETMIKO_DEVICE_TYPE = next(t for t in NETMIKO_DEVICE_TYPES if t in CLASS_MAPPER)

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

# Same, as a single pattern for Netmiko's send_config_set(error_pattern=...)
CONFIG_ERROR_PATTERN = (
    r"% (?:Invalid input|Incomplete command|Ambiguous command|Unrecognized)|% Error"
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

        * ``save_config`` (bool, default ``True``): run ``save config`` after
          a successful ``commit_config``.
        * ``startup_config_file`` (str, default ``/intflash/config.cfg``):
          the file returned as the ``startup`` configuration by ``get_config``.
        * ``config_method`` (``"cli"`` or ``"file"``, default ``"cli"``): how
          ``commit_config`` applies a merge candidate. ``"cli"`` sends the lines
          in configuration mode. ``"file"`` works like the IOS driver: the
          candidate is copied to the switch with SCP on load, a restore point is
          saved with ``save config file`` and the file is applied with
          ``source``.
        * ``dest_file_system`` (default ``/intflash``), ``merge_cfg`` (default
          ``napalm_merge.cfg``) and ``rollback_cfg`` (default
          ``napalm_rollback.cfg``): files used by ``config_method="file"``.
        """
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        optional_args = optional_args or {}

        self.save_on_commit = bool(optional_args.get("save_config", True))
        self.startup_config_file = optional_args.get("startup_config_file", "/intflash/config.cfg")
        self.force_no_enable = bool(optional_args.get("force_no_enable", False))
        self.config_method = optional_args.get("config_method", "cli")
        if self.config_method not in ("cli", "file"):
            raise ValueError("config_method must be 'cli' or 'file'")
        self.dest_file_system = optional_args.get("dest_file_system", "/intflash").rstrip("/")
        self.merge_cfg = optional_args.get("merge_cfg", "napalm_merge.cfg")
        self.rollback_cfg = optional_args.get("rollback_cfg", "napalm_rollback.cfg")
        self.netmiko_optional_args = netmiko_args(optional_args)

        self.device: Any = None
        self.candidate_config: Optional[str] = None
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
        output = ""
        try:
            for cmd in commands:
                output = self.device.send_command(cmd, **kwargs)
                output = output if isinstance(output, str) else str(output)
                if not self._has_cli_error(output):
                    break
        except (socket.error, EOFError) as exc:
            raise ConnectionClosedException(str(exc)) from exc
        return self._send_command_postprocess(output)

    @staticmethod
    def _send_command_postprocess(output: str) -> str:
        """Remove CLI noise from command output.

        The "Command Execution Time" banner is kept: it is used to compute
        ``last_flapped``.
        """
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
            config["running"] = self._clean_config(self._send_command(command))
        if retrieve in ("all", "startup"):
            config["startup"] = self._clean_config(
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
        if self.config_method == "file":
            self._upload_candidate(self.candidate_config)

    def _gen_full_path(self, filename: str) -> str:
        return f"{self.dest_file_system}/{filename}"

    def _upload_candidate(self, candidate: str) -> None:
        """Copy the candidate to the switch, wrapped like a saved config file."""
        content = candidate
        if not re.match(r"^\s*conf(?:ig(?:ure)?)?\s+t(?:erminal)?\b", content, re.I):
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
                dest_file=self.merge_cfg,
                file_system=self.dest_file_system,
            ) as transfer:
                transfer.transfer_file()
                if not transfer.verify_file():
                    raise MergeConfigException("File transfer to the switch failed")
        except MergeConfigException:
            raise
        except Exception as exc:
            raise MergeConfigException(f"File transfer to the switch failed: {exc}") from exc
        finally:
            os.remove(tmp_file)

    def load_replace_candidate(
        self, filename: Optional[str] = None, config: Optional[str] = None
    ) -> None:
        """Configuration replace is not supported by Fabric Engine."""
        raise ReplaceConfigException(
            "Fabric Engine does not support an atomic configuration replace; "
            "use load_merge_candidate instead."
        )

    def compare_config(self) -> str:
        """Return an incremental diff of the merge candidate.

        Fabric Engine has no on-box config diff, so (like the IOS driver's
        merge mode) the candidate is compared with the running configuration:
        only lines that would change something are returned. Lines that are
        not present yet are prefixed with ``+``; ``no <x>`` for an existing
        ``<x>`` is shown as ``-<x>``. Context lines (``interface ...``) are
        shown without a prefix.
        """
        if not self.candidate_config:
            return ""
        running = self._send_command("show running-config")
        return parsers.merge_diff(running, self.candidate_config)

    def commit_config(self, message: str = "", revert_in: Optional[int] = None) -> None:
        """Merge the candidate into the running configuration.

        The lines are sent in configuration mode; the first rejected line
        stops the merge and raises ``MergeConfigException`` (lines before it
        stay applied, as Fabric Engine has no rollback). The configuration is
        saved afterwards unless ``save_config`` is ``False``.
        """
        if message:
            raise NotImplementedError("Commit message not implemented for this platform")
        if revert_in is not None:
            raise NotImplementedError(
                "Commit confirm (revert_in) not implemented for this platform"
            )
        if not self.candidate_config:
            raise MergeConfigException("No candidate configuration loaded")

        if self.config_method == "file":
            self._commit_from_file()
        else:
            self._commit_from_cli()

        if self.save_on_commit:
            save_output = self.device.save_config()
            if self._has_cli_error(save_output):
                raise CommandErrorException(f"Saving the configuration failed:\n{save_output}")
        self.candidate_config = None

    def _gen_rollback_cfg(self) -> None:
        """Save the running config as a restore point (like the IOS driver)."""
        cmd = f"save config file {self._gen_full_path(self.rollback_cfg)}"
        output = self._send_command(cmd)
        if self._has_cli_error(output):
            raise MergeConfigException(f"Could not save the restore point:\n{output}")

    def _commit_from_file(self) -> None:
        self._gen_rollback_cfg()
        cmd = f"source {self._gen_full_path(self.merge_cfg)}"
        output = self._send_command(cmd, read_timeout=max(self.timeout, 120))
        if self._has_cli_error(output):
            raise MergeConfigException(
                "Configuration merge failed; the configuration before the commit was "
                f"saved to {self._gen_full_path(self.rollback_cfg)}:\n{output}"
            )

    def _commit_from_cli(self) -> None:
        lines = (self.candidate_config or "").splitlines()
        try:
            output = self.device.send_config_set(
                lines, cmd_verify=False, error_pattern=CONFIG_ERROR_PATTERN
            )
        except ConfigInvalidException as exc:
            try:
                self.device.exit_config_mode()
            except Exception:  # pragma: no cover - best effort
                pass
            raise MergeConfigException(f"Configuration merge failed:\n{exc}") from exc
        if self._has_cli_error(output):
            raise MergeConfigException(f"Configuration merge failed:\n{output}")

    def rollback(self) -> None:
        """Not supported: Fabric Engine cannot replace the running config.

        With ``config_method="file"`` the configuration before each commit is
        saved to ``rollback_cfg`` and can be restored manually (e.g. by booting
        from that file).
        """
        raise NotImplementedError(
            "Fabric Engine has no configuration replace; restore "
            f"{self._gen_full_path(self.rollback_cfg)} manually if needed"
        )

    def discard_config(self) -> None:
        """Discard the pending candidate."""
        self.candidate_config = None
