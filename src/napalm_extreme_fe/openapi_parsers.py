"""Converters from Fabric Engine OpenAPI responses to NAPALM structures.

Pure functions like ``parsers.py``: they take decoded JSON and return the
same structures as the CLI path. Field names and formats are from real
responses of Fabric Engine 9.4 (OpenAPI server 0.1.13.12, schema 0.2.0).
"""

from typing import Any, Dict, List

from napalm_extreme_fe.parsers import parse_os_version

# sysCapSupported / sysCapEnabled values -> NAPALM capability names (the CLI
# path maps the letters of the same LLDP capabilities)
LLDP_CAPABILITIES = {
    "OTHER": "other",
    "REPEATER": "repeater",
    "BRIDGE": "bridge",
    "WLAN_ACCESS_POINT": "wlan-access-point",
    "ROUTER": "router",
    "TELEPHONE": "telephone",
    "DOCSIS_CABLE_DEVICE": "docsis-cable-device",
    "STATION": "station",
}

DEFAULT_VRF = "GlobalRouter"


def port_name(name: str) -> str:
    """API port name -> CLI / NAPALM name: ``1:2`` -> ``1/2`` (``cpp`` unchanged)."""
    return name.replace(":", "/")


def facts(system: Dict, dns: Dict, ports: List[Dict]) -> Dict[str, Any]:
    """``state/system``, ``configuration/dns`` and ``v1/state/ports``."""
    hostname = system.get("sysName", "")
    card = (system.get("cards") or [{}])[0]
    domains = [d["name"] for d in dns.get("domains", []) if d.get("name") and not d.get("dynamic")]
    uptime = card.get("sysUpTime")  # 1/100 s
    return {
        "hostname": hostname,
        "fqdn": f"{hostname}.{domains[0]}" if hostname and domains else hostname,
        "vendor": "Extreme Networks",
        "model": card.get("modelName", ""),
        "serial_number": card.get("serialNumber", ""),
        "os_version": parse_os_version(system.get("sysDescription", "")),
        "uptime": float(uptime // 100) if isinstance(uptime, int) else -1.0,
        "interface_list": [
            p["settings"]["localName"]
            for p in ports
            if p.get("settings", {}).get("type") == "ETHERNET"
        ],
    }


def _capabilities(values: List[str]) -> List[str]:
    return sorted({LLDP_CAPABILITIES[v] for v in values if v in LLDP_CAPABILITIES})


def lldp_neighbors_detail(lldp: Dict) -> Dict[str, List[Dict]]:
    """``state/lldp`` -> ``{local port: [NAPALM detail dict]}``."""
    result: Dict[str, List[Dict]] = {}
    for neighbor in lldp.get("neighbors", []):
        result.setdefault(port_name(neighbor.get("portName", "")), []).append(
            {
                "parent_interface": "",
                "remote_port": neighbor.get("portId", ""),
                "remote_port_description": neighbor.get("portDesc", ""),
                "remote_chassis_id": neighbor.get("chassisId", ""),
                "remote_system_name": neighbor.get("sysName", ""),
                "remote_system_description": neighbor.get("sysDesc", ""),
                "remote_system_capab": _capabilities(neighbor.get("sysCapSupported", [])),
                "remote_system_enable_capab": _capabilities(neighbor.get("sysCapEnabled", [])),
            }
        )
    return result


def lldp_neighbors(lldp: Dict) -> Dict[str, List[Dict]]:
    """``state/lldp`` -> ``{local port: [{"hostname", "port"}]}`` (like the CLI path)."""
    return {
        port: [
            {
                "hostname": n["remote_system_name"] or n["remote_chassis_id"],
                "port": n["remote_port"],
            }
            for n in neighbors
        ]
        for port, neighbors in lldp_neighbors_detail(lldp).items()
    }


def arp_table(arp: List[Dict], vrf: str = "") -> List[Dict]:
    """``state/forwarding/ip-arp`` (all VRFs) -> the entries of one VRF.

    ``vrf`` is compared case-insensitively: the switch stores VRF names in
    lower case. The switch's own addresses (port ``cpp``) are reported on
    their VLAN interface, broadcast entries are skipped; the TTL is not an
    age, so ``age`` is -1 (as on the CLI path).
    """
    wanted = (vrf or DEFAULT_VRF).lower()
    entries: List[Dict] = []
    for table in arp:
        if table.get("vrName", "").lower() != wanted:
            continue
        for entry in table.get("arpList", []):
            mac = entry.get("physicalAddress", "")
            if mac.lower() == "ff:ff:ff:ff:ff:ff":
                continue
            port = entry.get("port", "")
            if port in ("cpp", "") and entry.get("vlanId"):
                interface = f"Vlan{entry['vlanId']}"
            else:
                interface = port_name(port)
            entries.append(
                {"interface": interface, "mac": mac, "ip": entry.get("ipAddress", ""), "age": -1.0}
            )
    return entries


def vrf_names(arp: List[Dict]) -> List[str]:
    """VRF names in a ``state/forwarding/ip-arp`` response."""
    return [table.get("vrName", "") for table in arp]


def mac_address_table(fdb: List[Dict]) -> List[Dict]:
    """``state/forwarding/mac-fdb`` -> NAPALM MAC table (``LEARNED`` = dynamic)."""
    return [
        {
            "mac": str(entry.get("macAddress", "")),
            "interface": port_name(entry.get("interfaceName", "")),
            "vlan": int(entry.get("vlanId", 0)),
            "static": entry.get("status") != "LEARNED",
            "active": True,
            "moves": -1,
            "last_move": -1.0,
        }
        for entry in fdb
    ]


def vlans(config: List[Dict], state: List[Dict]) -> Dict[int, Dict]:
    """``configuration/vlan`` (names) and ``v1/state/vlan`` (member ports).

    Lists every VLAN the API reports, also the internal ones ``show vlan
    basic`` hides (4092 ``Mgmt``, 4094 ``Null`` with the unassigned ports).
    """
    members = {
        v["vlanId"]: [
            port_name(i["interfaceName"])
            for i in v.get("memberInterfaces", [])
            if i.get("interfaceType") == "PORT"
        ]
        for v in state
    }
    return {
        v["id"]: {"name": v.get("name", ""), "interfaces": members.get(v["id"], [])} for v in config
    }


def ntp_servers(ntp: Dict) -> Dict[str, Dict]:
    """``configuration/ntp``: the servers of all VRFs (also those the cloud agent set)."""
    return {
        server["ipAddress"]["address"]: {}
        for settings in ntp.get("vrBasedSettings", [])
        for server in settings.get("servers", [])
        if server.get("ipAddress", {}).get("address")
    }


def snmp_information(snmp: Dict, system: Dict) -> Dict[str, Any]:
    """``configuration/snmp`` and ``state/system``.

    Unlike the CLI, the API returns the SNMPv1/v2c community strings. An
    entry with a write view is ``rw``, otherwise ``ro``; there is no ACL.
    """
    card = (system.get("cards") or [{}])[0]
    return {
        "chassis_id": card.get("serialNumber", ""),
        "contact": snmp.get("contact", ""),
        "location": snmp.get("location", ""),
        "community": {
            access["community"]: {
                "acl": "N/A",
                "mode": "rw" if access.get("writeViewName") else "ro",
            }
            for access in snmp.get("v1v2", {}).get("access", [])
            if access.get("community")
        },
    }
