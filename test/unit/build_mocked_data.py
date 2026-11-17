"""Build test/unit/mocked_data from the raw CLI captures in cli_samples/.

Usage:  python test/unit/build_mocked_data.py [--expected]

Each NAPALM getter test reads ``mocked_data/<test>/<case>/<sanitized cmd>.txt``.
Raw captures live in ``cli_samples/<case>/`` (one file per command, spaces and
``/`` replaced by ``_``). For every case directory, each test whose commands
are all present in that directory gets a test case. With ``--expected`` it also (re)generates
``expected_result.json`` by running the driver -- review the result by hand!
"""

import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "cli_samples")
MOCKED = os.path.join(HERE, "mocked_data")

SYS = "show sys-info"
INTF = "show interfaces gigabitEthernet"
RUN = "show running-config"
STARTUP = "more /intflash/config.cfg"

TESTS = {
    "test_get_facts": [SYS, INTF],
    "test_get_environment": [SYS],
    "test_get_interfaces": [INTF, "?show interfaces gigabitEthernet name"],
    "test_get_interfaces_ip": ["show ip interface vrfids 0-512", "show ipv6 address interface"],
    "test_get_interfaces_counters": [
        "show interfaces gigabitEthernet statistics verbose",
        "show interfaces gigabitEthernet error",
        "show interfaces gigabitEthernet statistics",
    ],
    "test_get_lldp_neighbors": ["show lldp neighbor"],
    "test_get_lldp_neighbors_detail": ["show lldp neighbor"],
    "test_get_arp_table": ["show ip arp"],
    "test_get_arp_table_with_vrf": ["show ip arp vrf TEST"],
    "test_get_mac_address_table": ["show vlan mac-address-entry"],
    "test_get_vlans": ["show vlan basic", "show vlan members"],
    "test_get_ntp_servers": [RUN],
    "test_get_snmp_information": [SYS, RUN],
    "test_get_config": [RUN, STARTUP],
    "test_get_config_filtered": [RUN, STARTUP],
    "test_get_config_sanitized": [RUN, STARTUP],
    "test_get_config_sanitized_filtered": [RUN, STARTUP],
    "test_is_alive": [],
}


# NAPALM's getter tests require non-empty results for some getters; captures
# from switches without data for them are tested in test_parsers.py instead.
EXCLUDE = {
    ("test_get_arp_table", "5320-48P-8XE_9.4.2.0"),
    ("test_get_interfaces_ip", "5320-48P-8XE_9.4.2.0"),
}


def sanitize(text):
    return re.sub("[^a-zA-Z0-9]", "_", text)[0:150]


def sample_name(command):
    return command.replace(" ", "_").replace("/", "_") + ".txt"


def main():
    built = []
    for case in sorted(os.listdir(SAMPLES)):
        sample_dir = os.path.join(SAMPLES, case)
        if not os.path.isdir(sample_dir):
            continue
        for test, commands in TESTS.items():
            if (test, case) in EXCLUDE:
                continue
            # commands prefixed with "?" are optional
            required = [c for c in commands if not c.startswith("?")]
            if not all(os.path.exists(os.path.join(sample_dir, sample_name(c))) for c in required):
                continue
            case_dir = os.path.join(MOCKED, test, case)
            os.makedirs(case_dir, exist_ok=True)
            for command in (c.lstrip("?") for c in commands):
                source = os.path.join(sample_dir, sample_name(command))
                if os.path.exists(source):
                    shutil.copy(source, os.path.join(case_dir, sanitize(command) + ".txt"))
            built.append((test, case))
    if "--expected" in sys.argv:
        for test, case in built:
            generate_expected(test, case)


def generate_expected(test, case):
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    sys.path.insert(0, HERE)
    from conftest import PatchedExtremeFEDriver

    calls = {
        "test_get_facts": lambda d: d.get_facts(),
        "test_get_environment": lambda d: d.get_environment(),
        "test_get_interfaces": lambda d: d.get_interfaces(),
        "test_get_interfaces_ip": lambda d: d.get_interfaces_ip(),
        "test_get_interfaces_counters": lambda d: d.get_interfaces_counters(),
        "test_get_lldp_neighbors": lambda d: d.get_lldp_neighbors(),
        "test_get_lldp_neighbors_detail": lambda d: d.get_lldp_neighbors_detail(),
        "test_get_arp_table": lambda d: d.get_arp_table(),
        "test_get_arp_table_with_vrf": lambda d: d.get_arp_table(vrf="TEST"),
        "test_get_mac_address_table": lambda d: d.get_mac_address_table(),
        "test_get_vlans": lambda d: d.get_vlans(),
        "test_get_ntp_servers": lambda d: d.get_ntp_servers(),
        "test_get_snmp_information": lambda d: d.get_snmp_information(),
        "test_get_config": lambda d: d.get_config(),
        "test_get_config_filtered": lambda d: d.get_config(retrieve="candidate"),
        "test_get_config_sanitized": lambda d: d.get_config(sanitized=True),
        "test_get_config_sanitized_filtered": lambda d: {
            "running": d.get_config(retrieve="running", sanitized=True)["running"],
            "startup": d.get_config(retrieve="startup", sanitized=True)["startup"],
            "candidate": "",
        },
        "test_is_alive": lambda d: d.is_alive(),
    }
    driver = PatchedExtremeFEDriver("host", "user", "pass")
    driver.device.current_test = test
    driver.device.current_test_case = case
    result = json.loads(json.dumps(calls[test](driver)))
    with open(os.path.join(MOCKED, test, case, "expected_result.json"), "w") as handle:
        json.dump(result, handle, indent=4)
        handle.write("\n")


if __name__ == "__main__":
    main()
