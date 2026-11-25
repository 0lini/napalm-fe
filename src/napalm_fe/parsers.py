"""Text parsers for Extreme Networks Fabric Engine (VOSS) CLI output.

All functions in this module are pure: they take raw CLI output and return
Python data structures. Keeping them separate from the driver makes them easy
to unit test without a device or a mocked SSH session.
"""

import ipaddress
import re
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple

IPV4_RE = r"\d{1,3}(?:\.\d{1,3}){3}"
MAC_RE = r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}"

# Fabric Engine LLDP capability letters -> NAPALM capability names. From the
# legend of "show lldp neighbor": B= Bridge, D= DOCSIS, O= Other,
# R= Repeater, S= Station, T= Telephone, W= WLAN, r= Router
LLDP_CAPABILITIES = {
    "O": "other",
    "R": "repeater",
    "B": "bridge",
    "W": "wlan-access-point",
    "r": "router",
    "T": "telephone",
    "D": "docsis-cable-device",
    "S": "station",
}

# Columns of the "show interfaces gigabitEthernet statistics [verbose]" and
# "... error" tables, mapped to NAPALM counter keys. Fabric Engine reports no
# transmit errors / discards (they stay -1).
COUNTER_COLUMNS = {
    "IN_OCTETS": "rx_octets",
    "OUT_OCTETS": "tx_octets",
    "IN_UNICST": "rx_unicast_packets",
    "OUT_UNICST": "tx_unicast_packets",
    "IN_MULTICST": "rx_multicast_packets",
    "OUT_MULTICST": "tx_multicast_packets",
    "IN_BRDCST": "rx_broadcast_packets",
    "OUT_BRDCST": "tx_broadcast_packets",
    "IN_DISCARD": "rx_discards",
}

# Receive error columns of the "error" table, summed up into rx_errors (there
# is no total)
RX_ERROR_COLUMNS = {"ERROR_ALIGN", "ERROR_FCS", "FRAMES_LONG", "TOO_SHORT"}

COUNTER_KEYS = (
    "tx_errors",
    "rx_errors",
    "tx_discards",
    "rx_discards",
    "tx_octets",
    "rx_octets",
    "tx_unicast_packets",
    "rx_unicast_packets",
    "tx_multicast_packets",
    "rx_multicast_packets",
    "tx_broadcast_packets",
    "rx_broadcast_packets",
)


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------


def _is_rule(line: str, char: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 5 and set(stripped) == {char}


def _is_footer(line: str) -> bool:
    stripped = line.strip().lower()
    return stripped.startswith("all ") or stripped.startswith("total ") or " out of " in stripped


def iter_tables(output: str) -> Iterator[Tuple[str, List[str], List[str]]]:
    """Split typical Fabric Engine tabular output into tables.

    Fabric Engine tables look like this::

        ==================================================
                           Table Title
        ==================================================
        COL1     COL2     COL3
        NUM      ...
        --------------------------------------------------
        row      row      row

        COL4     COL5            <- untitled sub-table
        --------------------------------------------------
        row      row

        All 2 out of 2 Total Num of ... displayed

    Handles sub-tables (sharing the title of the table above), dashed lines
    directly under the title, dashed end markers and footers.
    Yields ``(title, header_lines, data_lines)`` tuples; blank lines are
    removed from ``data_lines``.
    """
    tables: List[Tuple[str, List[str], List[str]]] = []
    title = ""
    header: List[str] = []
    buf: List[str] = []
    state = "idle"  # idle -> title -> header -> data

    def emit(rows: List[str]) -> None:
        tables.append((title, header, [r for r in rows if r.strip()]))

    for line in output.splitlines():
        if _is_rule(line, "="):
            if state == "data":
                emit(buf)
            if state == "title":
                state, header = "header", []
            else:
                state, title = "title", ""
            continue
        if _is_rule(line, "-"):
            if state == "header":
                if header:
                    state, buf = "data", []
                continue
            if state == "data":
                while buf and not buf[-1].strip():
                    buf.pop()
                blanks = [i for i, row in enumerate(buf) if not row.strip()]
                if blanks:
                    # rows after the last blank line are the header of a sub-table
                    emit(buf[: blanks[-1]])
                    header, buf = buf[blanks[-1] + 1 :], []
                else:
                    emit(buf)
                    state, header, buf = "header", [], []
            continue
        if state == "title":
            if line.strip():
                title = line.strip()
        elif state == "header":
            if line.strip():
                header.append(line)
        elif state == "data":
            if _is_footer(line):
                emit(buf)
                state, buf = "idle", []
                continue
            buf.append(line)
    if state == "data":
        emit(buf)
    return iter(tables)


def header_columns(header: List[str], tolerance: int = 3) -> List[str]:
    """Merge multi-line table headers into column names.

    ``["PORT     IN        OUT", "NUM      OCTETS    OCTETS"]`` ->
    ``["PORT_NUM", "IN_OCTETS", "OUT_OCTETS"]``

    Words are grouped into columns by their start position (within
    ``tolerance`` characters) and joined with ``_`` in line order.
    """
    columns: List[Tuple[int, List[str]]] = []
    for line in header:
        for match in re.finditer(r"\S+", line):
            pos = match.start()
            for col_pos, words in columns:
                if abs(col_pos - pos) <= tolerance:
                    words.append(match.group())
                    break
            else:
                columns.append((pos, [match.group()]))
    columns.sort(key=lambda c: c[0])
    return ["_".join(words).upper() for _, words in columns]


def tables_by_title(output: str, title: str) -> List[Tuple[str, List[str], List[str]]]:
    """Return the tables whose title matches ``title`` (case insensitive)."""
    return [t for t in iter_tables(output) if t[0].lower() == title.lower()]


def _switch_time(output: str, prefix: str) -> Optional[datetime]:
    """``<prefix> Fri Nov 20 16:31:55 2026 UTC`` -> datetime (without the zone)."""
    match = re.search(prefix + r"\w+\s+(\w+\s+\d+\s+\d+:\d+:\d+\s+\d{4})", output)
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.group(1).split()), "%b %d %H:%M:%S %Y")
    except ValueError:
        return None


def command_execution_time(output: str) -> Optional[datetime]:
    """Parse the ``Command Execution Time`` banner printed by Fabric Engine."""
    return _switch_time(output, r"Command Execution Time:\s+")


def parse_clock(output: str) -> Optional[datetime]:
    """Parse ``show clock`` (``Fri Nov 20 16:31:55 2026 UTC``, Fabric Engine 9.4)."""
    return _switch_time(output, r"^\s*")


def _rows_of(output: str, title: str) -> List[str]:
    """Rows of the tables titled ``title``; all rows if no table has a title."""
    tables = list(iter_tables(output))
    if any(t[0] for t in tables):
        tables = [t for t in tables if t[0].lower() == title.lower()]
    return [row for _, _, data in tables for row in data]


def table_rows(output: str) -> List[str]:
    """Return the data lines of every table in ``output``."""
    return [row for _, _, data in iter_tables(output) for row in data]


def _to_int(value: str, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def netmask_to_prefixlen(netmask: str) -> int:
    """Convert ``255.255.255.0`` into ``24``."""
    return sum(bin(int(octet)).count("1") for octet in netmask.split("."))


# --------------------------------------------------------------------------
# Command specific parsers
# --------------------------------------------------------------------------


def parse_os_version(sys_descr: str) -> str:
    """Extract the software version from the SysDescr string.

    ``5520-24T (9.0.0.0_B020) BoxType: 5520-24T`` -> ``9.0.0.0_B020``
    """
    match = re.search(r"\(([\w.\-]+)\)", sys_descr)
    return match.group(1) if match else ""


def parse_interfaces(output: str) -> Dict[str, Dict]:
    """Parse ``show interfaces gigabitEthernet``.

    Columns: PORT NUM, INDEX, DESCRIPTION, LINK TRAP, PORT LOCK, MTU,
    PHYSICAL ADDRESS, ADMIN STATUS, OPERATE STATUS.
    """
    result: Dict[str, Dict] = {}
    for row in _rows_of(output, "Port Interface"):
        match = re.match(
            r"^(?P<port>\d+/\d+(?:/\d+)?)\s+\d+\s+.*?\s+"
            r"(?P<mtu>\d+)\s+(?P<mac>" + MAC_RE + r")\s+"
            r"(?P<admin>up|down)\s+(?P<oper>up|down)",
            row.strip(),
            re.IGNORECASE,
        )
        if not match:
            continue
        result[match.group("port")] = {
            "mtu": int(match.group("mtu")),
            "mac_address": match.group("mac"),
            "is_enabled": match.group("admin").lower() == "up",
            "is_up": match.group("oper").lower() == "up",
        }
    return result


def parse_interfaces_name(output: str) -> Dict[str, Dict]:
    """Parse the ``Port Name`` table of ``show interfaces gigabitEthernet``.

    Columns: PORT NUM, NAME, DESCRIPTION, OPERATE STATUS, OPERATE DUPLEX,
    OPERATE SPEED, VLAN. The NAME column is optional and may contain spaces,
    so the line is parsed from both ends.
    """
    result: Dict[str, Dict] = {}
    for row in _rows_of(output, "Port Name"):
        tokens = row.split()
        if len(tokens) < 6 or not re.match(r"^\d+/\d+", tokens[0]):
            continue
        port = tokens[0]
        speed = _to_int(tokens[-2], default=0)
        result[port] = {"name": " ".join(tokens[1:-5]), "speed": float(speed)}
    return result


def parse_port_state(output: str) -> Dict[str, datetime]:
    """Parse the ``Port State`` table: time of the last port state change."""
    result: Dict[str, datetime] = {}
    for row in _rows_of(output, "Port State"):
        match = re.match(
            r"^(\d+/\d+(?:/\d+)?)\s+\S+\s+\S+\s+.*?(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})",
            row.strip(),
        )
        if not match:
            continue
        try:
            result[match.group(1)] = datetime.strptime(match.group(2), "%m/%d/%y %H:%M:%S")
        except ValueError:
            continue
    return result


def parse_ip_interfaces(output: str) -> Dict[str, Dict]:
    """Parse ``show ip interface``.

    Returns ``{interface: {"ipv4": {address: {"prefix_length": n}}}}``.
    """
    result: Dict[str, Dict] = {}
    for row in table_rows(output):
        match = re.match(
            r"^(?P<intf>\S+)\s+(?P<ip>" + IPV4_RE + r")\s+(?P<mask>" + IPV4_RE + r")",
            row.strip(),
        )
        if not match:
            continue
        intf = match.group("intf")
        entry = result.setdefault(intf, {"ipv4": {}})
        entry["ipv4"][match.group("ip")] = {
            "prefix_length": netmask_to_prefixlen(match.group("mask"))
        }
    return result


def parse_ipv6_interfaces(output: str) -> Dict[str, Dict]:
    """Parse ``show ipv6 address interface``.

    Rows look like (Fabric Engine 9.3)::

        IPV6 ADDRESS/PREFIX LENGTH        VID/BID/TID  TYPE    ORIGIN    STATUS ...
        2001:db8:10:0:0:0:0:1/64          V-10         UNICAST MANUAL    PREFERRED ...

    ``V-<vid>`` is returned as ``Vlan<vid>``, the name ``show ip interface``
    uses, so both address families end up under the same interface; other
    values (``B-``/``T-``, not seen yet) are kept. Addresses are returned in
    their compressed form (``2001:db8:10::1``).
    """
    result: Dict[str, Dict] = {}
    for row in table_rows(output):
        tokens = row.split()
        if len(tokens) < 2 or ":" not in tokens[0]:
            continue
        address, _, plen = tokens[0].partition("/")
        if not plen.isdigit():
            continue
        try:
            address = ipaddress.IPv6Address(address).compressed
        except ValueError:
            continue
        intf = re.sub(r"^V-(\d+)$", r"Vlan\1", tokens[1])
        entry = result.setdefault(intf, {"ipv6": {}})
        entry.setdefault("ipv6", {})[address] = {"prefix_length": int(plen)}
    return result


def parse_interface_counters(output: str) -> Dict[str, Dict[str, int]]:
    """Parse the per-port counter tables.

    Works with any table whose first header line contains known counter
    column names (see ``COUNTER_COLUMNS``), e.g. the outputs of
    ``show interfaces gigabitEthernet statistics`` and
    ``show interfaces gigabitEthernet error``.
    """
    result: Dict[str, Dict[str, int]] = {}
    for _, header, data in iter_tables(output):
        columns = header_columns(header)
        if not columns or not columns[0].startswith("PORT"):
            continue
        for row in data:
            tokens = row.split()
            if len(tokens) != len(columns) or not re.match(r"^\d+/\d+", tokens[0]):
                continue
            counters = result.setdefault(tokens[0], {k: -1 for k in COUNTER_KEYS})
            for column, value in zip(columns[1:], tokens[1:], strict=True):
                if not value.isdigit():
                    continue
                key = COUNTER_COLUMNS.get(column)
                if key:
                    counters[key] = int(value)
                elif column in RX_ERROR_COLUMNS:
                    counters["rx_errors"] = max(counters["rx_errors"], 0) + int(value)
    return result


def split_sections(output: str) -> Dict[str, List[str]]:
    """Split ``show sys-info`` into its sections.

    A section starts with a non-indented line ending in ``:`` such as
    ``Temperature Info :`` or ``Advanced Feature Bandwidth Reservation:``.
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[List[str]] = None
    for line in output.splitlines():
        match = re.match(r"^(\S.*?)\s*:\s*$", line)
        if match:
            current = sections.setdefault(match.group(1).strip(), [])
            continue
        if current is not None:
            current.append(line)
    return sections


def _threshold(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


def parse_sys_info_temperature(output: str) -> Dict[str, Dict]:
    """Parse the ``Temperature Info`` section of ``show sys-info``.

    Rows: ``<description> <temperature> <warning threshold> <critical threshold>``
    where thresholds may be ``N/A``. Sensors without a description (seen on the
    Fabric Engine VM) are named ``Sensor <n>`` by their position.
    """
    result: Dict[str, Dict] = {}
    position = 0
    for line in split_sections(output).get("Temperature Info", []):
        match = re.match(r"^\s*(.*?)\s*(?<!\S)(\d+(?:\.\d+)?)\s+(\S+)\s+(\S+)\s*$", line)
        if not match:
            continue
        position += 1
        name = match.group(1).strip() or f"Sensor {position}"
        temperature = float(match.group(2))
        warning = _threshold(match.group(3))
        critical = _threshold(match.group(4))
        is_critical = critical is not None and temperature >= critical
        result[name] = {
            "temperature": temperature,
            "is_alert": is_critical or (warning is not None and temperature >= warning),
            "is_critical": is_critical,
        }
    return result


def parse_sys_info_fans(output: str) -> Dict[str, Dict]:
    """Parse the ``Fan Info`` section of ``show sys-info``.

    Rows: ``<description> <oper status> <oper speed> <rpm> <airflow>``.
    """
    result: Dict[str, Dict] = {}
    for line in split_sections(output).get("Fan Info", []):
        match = re.match(r"^\s*(.+?)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\S+)\s*$", line)
        if not match:
            continue
        result[match.group(1)] = {"status": match.group(2).lower() == "up"}
    return result


def parse_sys_info_power(output: str) -> Dict[str, Dict]:
    """Parse the ``Power Supply Info`` section of ``show sys-info``.

    Per-PSU output and capacity are not reported, so they are set to -1.0.
    """
    result: Dict[str, Dict] = {}
    for line in split_sections(output).get("Power Supply Info", []):
        match = re.match(r"^\s*(Ps#\d+)\s+Status\s*:\s*(\S+)", line)
        if not match:
            continue
        result[match.group(1)] = {
            "status": match.group(2).lower() == "up",
            "capacity": -1.0,
            "output": -1.0,
        }
    return result


def parse_khi_cpu(output: str) -> Dict[int, Dict[str, float]]:
    """Parse ``show khi performance cpu``.

    Returns ``{slot: {"%usage": current utilization}}``.
    """
    result: Dict[int, Dict[str, float]] = {}
    slot: Optional[int] = None
    for line in output.splitlines():
        slot_match = re.match(r"^\s*Slot\s*:\s*(\d+)", line)
        if slot_match:
            slot = int(slot_match.group(1))
            continue
        usage = re.match(r"^\s*Current utilization\s*:\s*(\d+(?:\.\d+)?)", line)
        if usage and slot is not None:
            result[slot] = {"%usage": float(usage.group(1))}
    return result


def parse_khi_memory(output: str) -> Dict[str, int]:
    """Parse ``show khi performance memory`` (summed over all slots).

    Returns NAPALM's memory dict in bytes: ``used_ram`` and ``available_ram``
    (total = used + free).
    """
    used = free = 0
    found = False
    for line in output.splitlines():
        match = re.match(r"^\s*(Used|Free)\s*:\s*(\d+)\s*\(KB\)", line)
        if not match:
            continue
        found = True
        if match.group(1) == "Used":
            used += int(match.group(2)) * 1024
        else:
            free += int(match.group(2)) * 1024
    if not found:
        return {"used_ram": -1, "available_ram": -1}
    return {"used_ram": used, "available_ram": used + free}


# Commands that enter a configuration context which is left with "exit".
# "router isis enable" is a plain command, "router isis" enters a context.
CONTEXT_RE = re.compile(
    r"^(?:interface\s+\S+(?:\s+\S+)?|router\s+(?:isis|ospf|rip|bgp(?:\s+\d+)?|vrf\s+\S+)"
    r"|mgmt\s+\S+(?:\s+\S+)?|logical-intf\s+.+|application|i-sid\s+\d+.*|ip\s+vrf\s+\S+)\s*$",
    re.IGNORECASE,
)
IGNORED_LINES = {"exit", "end", "config terminal", "configure terminal", "enable"}


def _normalize(line: str) -> str:
    return " ".join(line.split()).lower()


def _config_contexts(config: str) -> Dict[str, List[str]]:
    """Map each context ("" for global) to its normalized lines.

    Fabric Engine does not indent context blocks; a context is entered by a
    line matching ``CONTEXT_RE`` and left by ``exit``.
    """
    contexts: Dict[str, List[str]] = {"": []}
    current = ""
    for raw in config.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        norm = _normalize(line)
        if norm == "exit":
            current = ""
            continue
        if norm in IGNORED_LINES:
            continue
        if not current and CONTEXT_RE.match(line):
            current = norm
            contexts.setdefault(current, [])
            contexts[""].append(norm)
            continue
        contexts.setdefault(current, []).append(norm)
    return contexts


def merge_diff(running_config: str, candidate: str) -> str:
    """Incremental diff of a merge candidate against the running config.

    * a candidate line already in the running config (same context) is not shown
    * a new line is shown as ``+line``
    * ``no <x>`` is shown as ``-<x>`` when ``<x>`` is configured, as ``+no <x>``
      when neither is configured, and not at all if ``no <x>`` is configured
    * context lines (``interface ...``) are shown unprefixed before their
      changes, or as ``+interface ...`` if the context itself is new
    """
    running = {ctx: set(lines) for ctx, lines in _config_contexts(running_config).items()}
    diff: List[str] = []
    context = ""
    context_line = ""
    context_shown = False

    def emit(entry: str) -> None:
        nonlocal context_shown
        if context and not context_shown:
            prefix = "" if context in running else "+"
            diff.append(f"{prefix}{context_line}")
            context_shown = True
        diff.append(entry)

    for raw in candidate.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        norm = _normalize(line)
        if norm == "exit":
            if context_shown:
                diff.append("exit")
            context, context_line, context_shown = "", "", False
            continue
        if norm in IGNORED_LINES:
            continue
        if not context and CONTEXT_RE.match(line):
            context, context_line, context_shown = norm, line, False
            if norm not in running:
                diff.append(f"+{line}")
                context_shown = True
            continue
        existing = running.get(context, set())
        if norm in existing:
            continue
        if norm.startswith("no "):
            positive = norm[3:]
            if positive in existing:
                emit(f"-{line[3:].strip()}")
                continue
        emit(f"+{line}")
    if context_shown:
        diff.append("exit")
    return "\n".join(diff)


def config_lines(config: str) -> List[str]:
    """Configuration lines without comments and blank lines."""
    return [
        line.rstrip()
        for line in config.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
