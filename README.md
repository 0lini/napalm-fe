# napalm-extreme-fe

[NAPALM](https://github.com/napalm-automation/napalm) community driver for
**Extreme Networks Fabric Engine** (formerly VOSS / VSP operating system),
running on the 5000/7000 series universal hardware and the VSP 4900/7400/8000 platforms.

The driver connects over SSH using Netmiko's `extreme_vsp` device type and parses
CLI output.

> **Status: alpha.** Parsers are developed against sample CLI output; please open
> an issue with the raw output if something does not parse on your platform/release.

## Installation

```bash
pip install napalm-extreme-fe
```

## Usage

```python
from napalm import get_network_driver

driver = get_network_driver("extreme_fe")
with driver("192.0.2.10", "rwa", "rwa", optional_args={"port": 22}) as device:
    print(device.get_facts())
    print(device.get_lldp_neighbors())
```

### Optional arguments

| Argument              | Default                | Description                                        |
|-----------------------|------------------------|----------------------------------------------------|
| `save_config`         | `True`                 | Run `save config` after a successful commit        |
| `startup_config_file` | `/intflash/config.cfg` | File returned as `startup` by `get_config()`       |
| `force_no_enable`     | `False`                | Do not enter privileged (`enable`) mode            |

All Netmiko connection arguments (`port`, `secret`, `key_file`, `use_keys`,
`ssh_config_file`, `conn_timeout`, ...) are passed through.

## Supported methods

| Method                       | Commands used                                                         |
|------------------------------|-----------------------------------------------------------------------|
| `get_facts`                  | `show sys-info`, `show interfaces gigabitEthernet`                    |
| `get_environment`            | `show sys-info`, `show khi performance cpu`, `... memory`             |
| `get_interfaces`             | `show interfaces gigabitEthernet` (+ `... name` on older releases)    |
| `get_interfaces_ip`          | `show ip interface vrfids 0-512`, `show ipv6 address interface`       |
| `get_interfaces_counters`    | `show interfaces gigabitEthernet statistics [verbose]`, `... error`   |
| `get_lldp_neighbors`         | `show lldp neighbor`                                                  |
| `get_lldp_neighbors_detail`  | `show lldp neighbor`                                                  |
| `get_arp_table`              | `show ip arp [vrf <name>]`                                            |
| `get_mac_address_table`      | `show vlan mac-address-entry`                                         |
| `get_vlans`                  | `show vlan basic`, `show vlan members`                                |
| `get_ntp_servers`            | `show running-config`                                                 |
| `get_snmp_information`       | `show sys-info`, `show running-config`                                |
| `get_config`                 | `show running-config [verbose]`, `more <startup file>`                |
| `cli`                        | any                                                                   |
| `is_alive`                   |                                                                       |

### Configuration management

| Method                   | Supported | Notes                                                                 |
|--------------------------|-----------|-----------------------------------------------------------------------|
| `load_merge_candidate`   | yes       | Candidate is kept in memory (no candidate datastore on the device)    |
| `compare_config`         | partial   | Returns the pending lines prefixed with `+`, not a real diff          |
| `commit_config`          | yes       | Sends the lines in config mode, then `save config` (see `save_config`) |
| `discard_config`         | yes       |                                                                       |
| `load_replace_candidate` | no        | Fabric Engine has no atomic config replace                            |
| `rollback`               | no        |                                                                       |

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Getter tests use the standard NAPALM test-suite (`napalm.base.test.getters`)
with mocked device output in `test/unit/mocked_data/`. Raw captures live in
`test/unit/cli_samples/<case>/` (one file per command, spaces and `/` replaced
with `_`); `normal/` holds hand-written samples, the other directories hold real
captures (trimmed) named after model and release.
To add or refresh them:

```bash
python test/unit/build_mocked_data.py --expected   # then review the expected_result.json diff!
```

## License

Apache 2.0
