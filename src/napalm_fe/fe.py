"""NAPALM driver for Extreme Networks Fabric Engine (formerly VOSS / VSP).

The driver talks to the switch's API (OpenAPI over HTTPS, Fabric Engine
9.4.2 or later without a license, 9.3.1 or later with a Premier license). It
never opens an SSH connection: getters read the API's JSON where it has the
data, everything else (and all configuration) goes through the API's CLI
operation.
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

from napalm_fe import openapi_parsers, parsers
from napalm_fe.openapi import DEFAULT_PORT, OpenApiSession

logger = logging.getLogger(__name__)

# Output that shows that the CLI rejected a command. Fabric Engine 9.4 also
# reports errors as e.g. "Error: The community entry already exists." (no
# "%"), sometimes after a BEL ("\x07Error: The specified VLAN ID is invalid or
# already in use", OpenAPI CLI operation).
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


class ConfigRejected(Exception):
    """The switch rejected or skipped a configuration line."""


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

        Supported ``optional_args``:

        * ``port`` (default ``9443``): port of the switch's API.
        * ``ssl_verify`` (default ``False``, like NX-OS): check the API's TLS
          certificate; ``True`` or the path of a CA bundle.
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

        self.port = int(optional_args.get("port", DEFAULT_PORT))
        self.ssl_verify: Union[bool, str] = optional_args.get("ssl_verify", False)
        self.save_on_commit = bool(optional_args.get("save_config", True))
        self.startup_config_file = optional_args.get("startup_config_file", "/intflash/config.cfg")

        self.api: Optional[OpenApiSession] = None
        self.candidate_config: Optional[str] = None
        self.profile = [self.platform]

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the API session (gets a token)."""
        self.api = OpenApiSession(
            self.hostname,
            self.username,
            self.password,
            port=self.port,
            verify=self.ssl_verify,
            timeout=self.timeout,
        )
        self.api.open()

    def close(self) -> None:
        """Close the API session."""
        if self.api is not None:
            self.api.close()
            self.api = None

    def is_alive(self) -> models.AliveDict:
        """Return whether the session holds a token (no request, like the EOS
        and NX-OS drivers)."""
        return {"is_alive": self.api is not None and self.api.token is not None}

    # ------------------------------------------------------------------
    # Transport primitives: everything that talks to the API is here.
    # ------------------------------------------------------------------

    def _api(self) -> OpenApiSession:
        if self.api is None:
            raise ConnectionClosedException("Not connected to the device, call open() first")
        return self.api

    def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET an OpenAPI path (below ``/rest/openapi``)."""
        return self._api().get(path, params)

    def _send_command(self, command: str) -> str:
        """Run a CLI command through ``POST /v0/operation/system/cli`` and
        return its post-processed output."""
        logger.debug("%s: sending %r", self.hostname, command)
        return self._send_command_postprocess(self._api().cli([command])[0])

    def _send_config(self, lines: List[str]) -> str:
        """``POST /v0/operation/system/cli`` with configuration lines.

        Each request starts in privileged EXEC mode, hence ``configure
        terminal`` / ``end``. Without ``continue_on_failure`` the switch skips
        every command after the first one it counts as failed (any output
        containing "Error"), so the merge stops at a rejected line (Fabric
        Engine 9.4). Raises ``ConfigRejected`` for a rejected or skipped line.
        """
        commands = ["configure terminal", *lines, "end"]
        outputs = self._api().cli(commands, continue_on_failure=False)
        ran = list(zip(commands, outputs, strict=False))
        transcript = "\n".join(f"{c}\n{o}".rstrip() for c, o in ran)
        failed = next((c for c, o in ran if self._has_cli_error(o)), None)
        if failed:
            raise ConfigRejected(f"Invalid input detected at command: {failed}\n{transcript}")
        if len(outputs) < len(commands):
            last = ran[-1][0] if ran else commands[0]
            raise ConfigRejected(f"The switch skipped the commands after: {last}\n{transcript}")
        return transcript

    def _copy_run_start(self, exc_class: type = CommandErrorException) -> None:
        """Save the running configuration (``save config``)."""
        try:
            self._api().save_config()
        except CommandErrorException as exc:
            raise exc_class(f"Saving the configuration failed: {exc}") from exc

    @staticmethod
    def _send_command_postprocess(output: str) -> str:
        """Remove CLI noise from command output (like the IOS driver).

        Removes the "Please widen the terminal" hint. The "Command Execution
        Time" banner is kept: it is used to compute ``last_flapped``.

        Log messages need no handling: ``logging screen`` is a setting of the
        CLI session (on for the serial console, off for every new SSH
        session, not stored in the configuration), and the driver never
        enables it.
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
        return openapi_parsers.facts(  # type: ignore[return-value]
            self._api_get("/v0/state/system"),
            self._api_get("/v0/configuration/dns"),
            self._api_get("/v1/state/ports"),
        )

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
        if now is None:  # the API's CLI output has no banner: use the switch's clock
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
        detail = openapi_parsers.lldp_neighbors_detail(self._api_get("/v0/state/lldp"))
        if interface:
            detail = {port: n for port, n in detail.items() if port == interface}
        return detail  # type: ignore[return-value]

    def get_arp_table(self, vrf: str = "") -> List[models.ARPTableDict]:
        """Return the ARP table, optionally for a given VRF."""
        # all VRFs in one response; vr_name=... would be case-sensitive
        arp = self._api_get("/v0/state/forwarding/ip-arp")
        if vrf and vrf.lower() not in {t.get("vrName", "").lower() for t in arp}:
            configured = self._api_get("/v0/configuration/vrf")
            if vrf.lower() not in {v.get("name", "").lower() for v in configured}:
                raise CommandErrorException(f"VRF {vrf!r} does not exist")
        return openapi_parsers.arp_table(arp, vrf)  # type: ignore[return-value]

    def get_mac_address_table(self) -> List[models.MACAdressTable]:
        """Return the MAC address table."""
        return openapi_parsers.mac_address_table(  # type: ignore[return-value]
            self._api_get("/v0/state/forwarding/mac-fdb")
        )

    def get_vlans(self) -> Dict[str, models.VlanDict]:
        """Return the configured VLANs and their member ports."""
        return openapi_parsers.vlans(  # type: ignore[return-value]
            self._api_get("/v0/configuration/vlan"), self._api_get("/v1/state/vlan")
        )

    def get_ntp_servers(self) -> Dict[str, models.NTPServerDict]:
        """Return the NTP servers the switch uses.

        The API also lists servers that are not in the running configuration,
        such as the one the cloud agent sets.
        """
        return openapi_parsers.ntp_servers(self._api_get("/v0/configuration/ntp"))  # type: ignore[return-value]

    def get_snmp_information(self) -> models.SNMPDict:
        """Return SNMP information, including the SNMPv1/v2c community strings
        (the CLI never displays them, the API does)."""
        return openapi_parsers.snmp_information(  # type: ignore[return-value]
            self._api_get("/v0/configuration/snmp"), self._api_get("/v0/state/system")
        )

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

        if self.save_on_commit:
            self._copy_run_start(CommandErrorException)
        self.candidate_config = None

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
        except ConfigRejected as exc:
            raise self._merge_failed(str(exc)) from exc
        if self._has_cli_error(output):
            raise self._merge_failed(output)

    def discard_config(self) -> None:
        """Discard the pending candidate."""
        self.candidate_config = None
