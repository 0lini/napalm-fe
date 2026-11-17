#!/usr/bin/env python3
"""Capture CLI output from a Fabric Engine switch as a new test case.

Runs every command the driver uses (see ``TESTS`` in
``test/unit/build_mocked_data.py``) and stores the raw output in
``test/unit/cli_samples/<case>/``. Afterwards run::

    python test/unit/build_mocked_data.py --expected

and review every new ``expected_result.json`` by hand before committing.

Only read-only ``show``/``ls`` commands are sent. Example::

    python tools/capture.py --host 192.0.2.10 --user rwa --case <model>_<release> \\
        --anonymize --anonymize-ips --vrf blue

``--anonymize`` replaces the hostname, domain name and serial numbers;
``--anonymize-ips`` maps IPv4 addresses to 10.0.0.0/8; ``--replace OLD=NEW``
replaces anything else (LLDP neighbour names, tunnel names, ...). ALWAYS
review the files before committing them - the anonymisation is best effort.
"""

import argparse
import getpass
import ipaddress
import os
import re
import sys
from typing import Callable, Dict, Iterable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UNIT = os.path.join(ROOT, "test", "unit")
sys.path.insert(0, ROOT)
sys.path.insert(0, UNIT)

from build_mocked_data import TESTS, sample_name  # noqa: E402

# Captured in addition to the getter commands (useful for new parsers)
EXTRA_COMMANDS = [
    "show interfaces gigabitEthernet name",
    "show khi performance cpu",
    "show khi performance memory",
    "show boot config flags",
    "ls /intflash",
    "ls /intflash/doesnotexist.cfg",
    "file-checksum md5 /intflash/config.cfg",
]

IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def commands_to_capture(vrf: Optional[str]) -> List[str]:
    """All unique commands, in a stable order."""
    commands: List[str] = []
    for cmds in TESTS.values():
        for cmd in cmds:
            cmd = cmd.lstrip("?")
            if cmd == "show ip arp vrf TEST":
                if not vrf:
                    continue
                cmd = f"show ip arp vrf {vrf}"
            if cmd not in commands:
                commands.append(cmd)
    for cmd in EXTRA_COMMANDS:
        if cmd not in commands:
            commands.append(cmd)
    return commands


def _is_special_ip(addr: ipaddress.IPv4Address) -> bool:
    text = str(addr)
    if addr.is_unspecified or addr.is_loopback or addr.is_multicast or text.startswith("255."):
        return True
    # netmasks such as 255.255.255.0 / wildcard masks 0.0.0.255
    as_int = int(addr)
    inverted = as_int ^ 0xFFFFFFFF
    return (inverted & (inverted + 1)) == 0 or (as_int & (as_int + 1)) == 0


def ip_anonymizer() -> Callable[[str], str]:
    """Return a function that consistently maps IPv4 addresses to 10.0.0.0/8.

    Version strings such as ``(9.4.2.0)`` or ``Version : 3.4.2.11`` are kept.
    """
    mapping: Dict[str, str] = {}

    def anonymize(text: str) -> str:
        lines = []
        for line in text.splitlines(keepends=True):
            if re.search(r"version", line, re.I):
                lines.append(line)
                continue

            def replace(match: "re.Match[str]") -> str:
                start = match.start(1)
                if start > 0 and match.string[start - 1] in "([":
                    return match.group(0)
                try:
                    addr = ipaddress.IPv4Address(match.group(1))
                except ValueError:
                    return match.group(0)
                if _is_special_ip(addr):
                    return match.group(0)
                if match.group(1) not in mapping:
                    mapping[match.group(1)] = str(ipaddress.IPv4Address(0x0A000001 + len(mapping)))
                return mapping[match.group(1)]

            lines.append(IPV4_RE.sub(replace, line))
        return "".join(lines)

    return anonymize


def find_identifiers(outputs: Dict[str, str]) -> Dict[str, str]:
    """Hostname, domain name and serial numbers to replace."""
    replacements: Dict[str, str] = {}
    sys_info = outputs.get("show sys-info", "")
    hostname = re.search(r"^\s*SysName\s*:\s*(\S+)", sys_info, re.M)
    if hostname:
        replacements[hostname.group(1)] = "fe-sw-01"
    running = outputs.get("show running-config", "")
    domain = re.search(r'^\s*ip domain-name\s+"?([^"\s]+)"?', running, re.M)
    if domain:
        replacements[domain.group(1)] = "example.com"
    serials = re.findall(
        r"(?:Serial#|Serial Number|CardSerial#)\s*:\s*([A-Za-z0-9][\w-]{4,})",
        sys_info + running,
    )
    for index, serial in enumerate(dict.fromkeys(serials)):
        replacements[serial] = f"SN{index:010d}"
    return replacements


def apply_replacements(text: str, replacements: Dict[str, str]) -> str:
    # longest first so that "sw-01.example.org" wins over "sw-01"
    for old in sorted(replacements, key=len, reverse=True):
        text = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", replacements[old], text)
    return text


def parse_replace(values: Iterable[str]) -> Dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--replace expects OLD=NEW, got {value!r}")
        old, new = value.split("=", 1)
        result[old] = new
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", help="default: prompt or $NAPALM_PASSWORD")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--case", required=True, help="test case name, e.g. <model>_<release>")
    parser.add_argument("--vrf", help="VRF to use for 'show ip arp vrf' (saved as vrf TEST)")
    parser.add_argument("--anonymize", action="store_true", help="hostname, domain, serials")
    parser.add_argument("--anonymize-ips", action="store_true", help="map IPv4 to 10.0.0.0/8")
    parser.add_argument("--replace", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("NAPALM_PASSWORD") or getpass.getpass()

    from napalm_extreme_fe import ExtremeFEDriver

    driver = ExtremeFEDriver(
        args.host, args.user, password, timeout=args.timeout, optional_args={"port": args.port}
    )
    driver.open()
    outputs: Dict[str, str] = {}
    try:
        for command in commands_to_capture(args.vrf):
            print(f"  {command}")
            outputs[command] = driver.device.send_command(command, read_timeout=args.timeout)
    finally:
        driver.close()

    replacements = parse_replace(args.replace)
    if args.anonymize:
        replacements = {**find_identifiers(outputs), **replacements}
    anonymize_ips = ip_anonymizer() if args.anonymize_ips else (lambda text: text)

    case_dir = os.path.join(UNIT, "cli_samples", args.case)
    os.makedirs(case_dir, exist_ok=True)
    for command, output in outputs.items():
        name = command
        if args.vrf and command == f"show ip arp vrf {args.vrf}":
            name = "show ip arp vrf TEST"
        text = anonymize_ips(apply_replacements(output, replacements))
        with open(os.path.join(case_dir, sample_name(name)), "w") as handle:
            handle.write(text.rstrip("\n") + "\n")

    print(f"\nSaved {len(outputs)} files to {case_dir}")
    if replacements:
        print("Replaced: " + ", ".join(f"{k} -> {v}" for k, v in replacements.items()))
    print(
        "\nNext steps:\n"
        "  1. review the files for anything sensitive (names, IPs, keys)\n"
        "  2. python test/unit/build_mocked_data.py --expected\n"
        "  3. review the new expected_result.json files against the raw output\n"
        "  4. pytest"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
