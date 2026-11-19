"""Text parsers for Extreme Networks Fabric Engine (VOSS) CLI output.

All functions in this module are pure: they take raw CLI output and return
Python data structures. Keeping them separate from the driver makes them easy
to unit test without a device or a mocked SSH session.
"""

import difflib
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


def command_execution_time(output: str) -> Optional[datetime]:
    """Parse the ``Command Execution Time`` banner printed by Fabric Engine."""
    match = re.search(r"Command Execution Time:\s+\w+\s+(\w+\s+\d+\s+\d+:\d+:\d+\s+\d{4})", output)
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.group(1).split()), "%b %d %H:%M:%S %Y")
    except ValueError:
        return None


def _rows_of(output: str, title: str) -> List[str]:
    """Rows of the tables titled ``title``; all rows if no table has a title."""
    tables = list(iter_tables(output))
    if any(t[0] for t in tables):
        tables = tables_by_title(output, title)
    rows: List[str] = []
    for _, _, data in tables:
        rows.extend(data)
    return rows


def table_rows(output: str) -> List[str]:
    """Return the data lines of every table in ``output``."""
    rows: List[str] = []
    for _, _, data in iter_tables(output):
        rows.extend(data)
    return rows


def parse_key_value(output: str) -> Dict[str, str]:
    """Parse ``Key : Value`` style output (e.g. ``show sys-info``).

    The first occurrence of a key wins.
    """
    result: Dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^\s*([^:]+?)\s*:\s?(.*)$", line)
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key and key not in result:
            result[key] = value
    return result


def parse_uptime(uptime: str) -> float:
    """Convert ``12 day(s), 03:14:15`` into seconds."""
    seconds = 0.0
    days = re.search(r"(\d+)\s*day", uptime)
    if days:
        seconds += int(days.group(1)) * 86400
    hms = re.search(r"(\d+):(\d+):(\d+)", uptime)
    if hms:
        hours, minutes, secs = (int(x) for x in hms.groups())
        seconds += hours * 3600 + minutes * 60 + secs
    if not days and not hms:
        return -1.0
    return float(seconds)


def _split_port(port: str) -> Tuple[str, int]:
    prefix, _, last = port.rpartition("/")
    return prefix, int(last)


def expand_port_list(ports: str) -> List[str]:
    """Expand a Fabric Engine port list into individual ports.

    ``"1/1-1/3,1/5,1/49/1-1/49/2"`` ->
    ``["1/1", "1/2", "1/3", "1/5", "1/49/1", "1/49/2"]``
    """
    result: List[str] = []
    ports = ports.strip()
    if not ports or ports in ("-", "none", "N/A"):
        return result
    for item in ports.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start, end = (x.strip() for x in item.split("-", 1))
            try:
                start_prefix, start_num = _split_port(start)
                end_prefix, end_num = _split_port(end)
            except ValueError:
                result.append(item)
                continue
            if start_prefix != end_prefix or end_num < start_num:
                result.extend([start, end])
                continue
            result.extend(f"{start_prefix}/{num}" for num in range(start_num, end_num + 1))
        else:
            result.append(item)
    return result


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


def parse_sys_info(output: str) -> Dict[str, str]:
    """Parse ``show sys-info``."""
    return parse_key_value(output)


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


def _parse_lldp_capabilities(raw: str) -> Tuple[List[str], List[str]]:
    raw = re.sub(r"\(.*?\)", "", raw).strip()
    if "/" in raw:
        supported, enabled = (x.strip() for x in raw.split("/", 1))
    else:
        supported = enabled = raw

    def _letters(value: str) -> List[str]:
        caps = []
        for letter in value:
            cap = LLDP_CAPABILITIES.get(letter)
            if cap and cap not in caps:
                caps.append(cap)
        return sorted(caps)

    return _letters(supported), _letters(enabled)


def parse_lldp_neighbors(output: str) -> List[Dict[str, str]]:
    """Parse ``show lldp neighbor`` (detailed, per neighbor blocks).

    Returns a list of dicts with the raw fields plus ``local_port``.
    """
    neighbors: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in output.splitlines():
        port_match = re.match(r"^\s*Port\s*:\s*(\S+)(.*)$", line)
        if port_match:
            current = {"local_port": port_match.group(1)}
            neighbors.append(current)
            rest = port_match.group(2)
            index = re.search(r"Index\s*:\s*(\S+)", rest)
            if index:
                current["Index"] = index.group(1)
            continue
        if _is_rule(line, "-") or _is_rule(line, "="):
            current = None  # a rule ends the neighbor block
            continue
        if current is None:
            continue
        kv = re.match(r"^\s*([A-Za-z][\w ]*?)\s*:\s*(.*)$", line)
        if kv:
            current.setdefault(kv.group(1).strip(), kv.group(2).strip())
    return neighbors


def lldp_neighbor_to_napalm(raw: Dict[str, str]) -> Dict:
    """Convert a raw LLDP neighbor block into a NAPALM detail dict."""

    def _last_token(value: str) -> str:
        tokens = value.split()
        return tokens[-1] if tokens else ""

    supported, enabled = _parse_lldp_capabilities(raw.get("SysCap", ""))
    return {
        "parent_interface": "",
        "remote_port": _last_token(raw.get("PortId", "")),
        "remote_port_description": raw.get("PortDescr", ""),
        "remote_chassis_id": _last_token(raw.get("ChassisId", "")),
        "remote_system_name": raw.get("SysName", ""),
        "remote_system_description": raw.get("SysDescr", ""),
        "remote_system_capab": supported,
        "remote_system_enable_capab": enabled,
    }


def parse_arp_table(output: str) -> List[Dict]:
    """Parse ``show ip arp``.

    Columns: IP_ADDRESS, MAC_ADDRESS, VLAN, PORT, TYPE, TTL, TUNNEL.
    """
    entries: List[Dict] = []
    for row in table_rows(output):
        match = re.match(
            r"^(?P<ip>" + IPV4_RE + r")\s+(?P<mac>" + MAC_RE + r")\s+"
            r"(?P<vlan>\S+)\s+(?P<port>\S+)",
            row.strip(),
        )
        if not match:
            continue
        if match.group("mac").lower() == "ff:ff:ff:ff:ff:ff":
            continue
        port, vlan = match.group("port"), match.group("vlan")
        if port in ("-", "--") and vlan.isdigit():
            interface = f"Vlan{vlan}"
        elif port in ("-", "--"):
            interface = ""
        else:
            interface = port
        entries.append(
            {
                "interface": interface,
                "mac": match.group("mac"),
                "ip": match.group("ip"),
                "age": -1.0,
            }
        )
    return entries


def parse_mac_address_table(output: str) -> List[Dict]:
    """Parse ``show vlan mac-address-entry``.

    Columns: VLAN ID, STATUS, MAC ADDRESS, INTERFACE, SMLT REMOTE, TUNNEL.
    Interfaces like ``Port-1/56`` or ``c1:1/3`` are normalised to ``1/56`` / ``1/3``.
    """
    entries: List[Dict] = []
    for row in table_rows(output):
        tokens = row.split()
        if len(tokens) < 4 or not tokens[0].isdigit() or not re.fullmatch(MAC_RE, tokens[2]):
            continue
        mac_type, mac, port = tokens[1], tokens[2], tokens[3]
        port = re.sub(r"^(?:[cu]\d*:|Port-)", "", port)
        if port in ("-", "--"):
            port = ""
        entries.append(
            {
                "mac": mac,
                "interface": port,
                "vlan": int(tokens[0]),
                "static": mac_type.lower() != "learned",
                "active": True,
                "moves": -1,
                "last_move": -1.0,
            }
        )
    return entries


def parse_vlan_basic(output: str) -> Dict[int, str]:
    """Parse ``show vlan basic``.

    VLAN names may contain spaces, so the number of columns after NAME is
    taken from the header.
    """
    vlans: Dict[int, str] = {}
    for _, header, data in iter_tables(output):
        columns = header_columns(header)
        if "NAME" not in columns:
            continue
        after_name = len(columns) - columns.index("NAME") - 1
        for row in data:
            tokens = row.split()
            if len(tokens) < after_name + 2 or not tokens[0].isdigit():
                continue
            vlans[int(tokens[0])] = " ".join(tokens[1 : len(tokens) - after_name])
    return vlans


def parse_vlan_members(output: str) -> Dict[int, List[str]]:
    """Parse ``show vlan members``.

    Port lists can be long and wrap onto continuation lines, so the column
    positions of the header are used to find the "PORT MEMBER" column.
    """
    members: Dict[int, List[str]] = {}
    for _, header, data in iter_tables(output):
        if not header:
            continue
        first = header[0]
        start = first.find("PORT")
        end = first.find("ACTIVE")
        if start < 0:
            continue
        if end <= start:
            end = None  # type: ignore[assignment]
        current: Optional[int] = None
        for row in data:
            vlan_match = re.match(r"^(\d+)\s", row)
            if vlan_match:
                current = int(vlan_match.group(1))
                members.setdefault(current, [])
            if current is None:
                continue
            column = row[start:end].strip() if len(row) > start else ""
            if column:
                members[current].extend(expand_port_list(column.rstrip(",")))
    return members


def parse_ntp_servers(running_config: str) -> List[str]:
    """Extract configured NTP servers from the running configuration."""
    servers: List[str] = []
    for line in running_config.splitlines():
        match = re.match(r"^\s*ntp server (\S+)", line)
        if match and match.group(1) not in servers:
            servers.append(match.group(1))
    return servers


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


def parse_domain_name(running_config: str) -> str:
    """Extract ``ip domain-name "example.com"`` from (a part of) the config."""
    match = re.search(r'^\s*ip domain-name\s+"?([^"\s]+)"?', running_config, re.M)
    return match.group(1) if match else ""


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


def _config_lines(config: str) -> List[str]:
    """Configuration lines without comments and blank lines."""
    return [
        line.rstrip()
        for line in config.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def replace_diff(running_config: str, candidate: str) -> str:
    """Line diff for a replace: ``-line`` (removed) / ``+line`` (added).

    ``running_config`` without banners (``ExtremeFeDriver._clean_config``).
    """
    diff = difflib.unified_diff(
        _config_lines(running_config), _config_lines(candidate), lineterm="", n=0
    )
    return "\n".join(line for line in diff if not line.startswith(("---", "+++", "@@")))


# Timestamp of a "show logging file" entry (RFC 5424 format):
#   1 2026-11-17T16:33:05.861Z 5520-24T-FabricEngine CP1 - 0x0003068e - 00000000 ...
LOG_ENTRY_RE = re.compile(
    r"^\s*1 (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|[+-]\d{2}:?\d{2})? "
    r"\S+ \S+ - 0x[0-9a-fA-F]+ - ",
    re.M,
)


def latest_log_time(output: str) -> Optional[datetime]:
    """Timestamp of the newest entry in ``show logging file`` output."""
    times = [
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(
            microsecond=int(fraction.ljust(6, "0")[:6]) if fraction else 0
        )
        for stamp, fraction in LOG_ENTRY_RE.findall(output)
    ]
    return max(times) if times else None
