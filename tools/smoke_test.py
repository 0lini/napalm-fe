#!/usr/bin/env python3
"""Smoke test the driver against a live Fabric Engine switch.

Read-only by default: every implemented getter is called and its result is
validated against NAPALM's data models. Example::

    python tools/smoke_test.py --host 192.0.2.10 --user rwa

``--test-config`` additionally runs a reversible configuration test: it sets
``snmp-server location "napalm-smoke-test"`` through load_merge_candidate /
compare_config / commit_config, verifies it, and restores the previous
location. The change is NOT saved to the startup config (``save_config``
is disabled). Use ``--config-method file`` to test the SCP + ``source`` path;
it leaves ``napalm_merge.cfg`` and ``napalm_rollback.cfg`` in /intflash.
"""

import argparse
import getpass
import json
import os
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from napalm.base import models  # noqa: E402
from napalm.base.test import helpers  # noqa: E402

from napalm_extreme_fe import ExtremeFEDriver  # noqa: E402


def _check_each(model: Any) -> Callable[[Any], bool]:
    def check(result: Any) -> bool:
        values = result.values() if isinstance(result, dict) else result
        return all(helpers.test_model(model, v) for v in values)

    return check


def _check_lldp_detail(result: Dict[str, List[Dict]]) -> bool:
    return all(
        helpers.test_model(models.LLDPNeighborDetailDict, n) for ns in result.values() for n in ns
    )


def _check_lldp(result: Dict[str, List[Dict]]) -> bool:
    return all(helpers.test_model(models.LLDPNeighborDict, n) for ns in result.values() for n in ns)


def _check_environment(result: Dict[str, Any]) -> bool:
    return (
        all(helpers.test_model(models.FanDict, v) for v in result["fans"].values())
        and all(
            helpers.test_model(models.TemperatureDict, v) for v in result["temperature"].values()
        )
        and all(helpers.test_model(models.PowerDict, v) for v in result["power"].values())
        and all(helpers.test_model(models.CPUDict, v) for v in result["cpu"].values())
        and helpers.test_model(models.MemoryDict, result["memory"])
    )


GETTERS: List[Tuple[str, Callable[[ExtremeFEDriver], Any], Callable[[Any], bool]]] = [
    ("get_facts", lambda d: d.get_facts(), lambda r: helpers.test_model(models.FactsDict, r)),
    ("get_environment", lambda d: d.get_environment(), _check_environment),
    ("get_interfaces", lambda d: d.get_interfaces(), _check_each(models.InterfaceDict)),
    ("get_interfaces_ip", lambda d: d.get_interfaces_ip(), lambda r: isinstance(r, dict)),
    (
        "get_interfaces_counters",
        lambda d: d.get_interfaces_counters(),
        _check_each(models.InterfaceCounterDict),
    ),
    ("get_lldp_neighbors", lambda d: d.get_lldp_neighbors(), _check_lldp),
    ("get_lldp_neighbors_detail", lambda d: d.get_lldp_neighbors_detail(), _check_lldp_detail),
    ("get_arp_table", lambda d: d.get_arp_table(), _check_each(models.ARPTableDict)),
    (
        "get_mac_address_table",
        lambda d: d.get_mac_address_table(),
        _check_each(models.MACAdressTable),
    ),
    ("get_vlans", lambda d: d.get_vlans(), _check_each(models.VlanDict)),
    ("get_ntp_servers", lambda d: d.get_ntp_servers(), lambda r: isinstance(r, dict)),
    (
        "get_snmp_information",
        lambda d: d.get_snmp_information(),
        lambda r: helpers.test_model(models.SNMPDict, r),
    ),
    (
        "get_config",
        lambda d: d.get_config(sanitized=True),
        lambda r: helpers.test_model(models.ConfigDict, r) and bool(r["running"]),
    ),
    ("is_alive", lambda d: d.is_alive(), lambda r: r == {"is_alive": True}),
]


def run_getters(device: ExtremeFEDriver, verbose: bool) -> int:
    failures = 0
    for name, call, check in GETTERS:
        try:
            result = call(device)
            ok = check(result)
        except Exception:  # noqa: BLE001 - report every failure
            print(f"[ERROR] {name}\n{traceback.format_exc()}")
            failures += 1
            continue
        size = len(result) if hasattr(result, "__len__") else "-"
        print(f"[{'OK' if ok else 'FAIL'}] {name} ({size} entries)")
        if not ok:
            failures += 1
        if verbose or not ok:
            print(json.dumps(result, indent=2, default=str)[:4000])
    return failures


def run_config_test(device: ExtremeFEDriver) -> int:
    original = device.get_snmp_information()["location"]
    marker = "napalm-smoke-test"
    print(f"\nConfig test ({device.config_method}): current location {original!r}")
    try:
        device.load_merge_candidate(config=f'snmp-server location "{marker}"')
        diff = device.compare_config()
        print(f"compare_config:\n{diff}")
        if f'+snmp-server location "{marker}"' not in diff:
            print("[FAIL] unexpected diff")
            return 1
        device.commit_config()
        now = device.get_snmp_information()["location"]
        if now != marker:
            print(f"[FAIL] location after commit is {now!r}")
            return 1
        print("[OK] commit applied")
    finally:
        restore = f'snmp-server location "{original}"' if original else "no snmp-server location"
        device.discard_config()
        device.load_merge_candidate(config=restore)
        device.commit_config()
        print(f"restored: {device.get_snmp_information()['location']!r}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", help="default: prompt or $NAPALM_PASSWORD")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--verbose", action="store_true", help="print every getter result")
    parser.add_argument("--test-config", action="store_true", help="run the reversible config test")
    parser.add_argument("--config-method", choices=("cli", "file"), default="cli")
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("NAPALM_PASSWORD") or getpass.getpass()
    optional_args = {
        "port": args.port,
        "config_method": args.config_method,
        "save_config": False,
    }
    with ExtremeFEDriver(
        args.host, args.user, password, timeout=args.timeout, optional_args=optional_args
    ) as device:
        failures = run_getters(device, args.verbose)
        if args.test_config:
            failures += run_config_test(device)
    print(f"\n{'PASSED' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
