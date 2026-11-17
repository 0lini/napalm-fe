# napalm-extreme-fe

[NAPALM](https://github.com/napalm-automation/napalm) community driver for
**Extreme Networks Fabric Engine** (formerly VOSS, the VSP Operating System
Software). It works with any switch running Fabric Engine / VOSS; the CLI is the
same across hardware platforms, so there is no model-specific code.

The driver connects over SSH with [Netmiko](https://github.com/ktbyers/netmiko)
and parses the CLI output. Netmiko still names the platform `extreme_vsp`; the
driver automatically uses the newest name Netmiko provides (`extreme_fe`, then
`extreme_voss`, then `extreme_vsp`).

> **Status: beta.** The getters are tested against real device captures (see
> `test/unit/cli_samples/`). Output formats can differ between releases - if
> something does not parse on your switch, please open an issue with the raw
> CLI output (see [Reporting parsing problems](#reporting-parsing-problems)).

## Installation

```bash
pip install napalm-extreme-fe
```

Requires Python 3.10+, NAPALM 5 and Netmiko 4.

## Usage

```python
from napalm import get_network_driver

driver = get_network_driver("extreme_fe")
with driver("192.0.2.10", "admin", "secret", optional_args={"port": 22}) as device:
    print(device.get_facts())
    print(device.get_lldp_neighbors())

    device.load_merge_candidate(config='snmp-server location "DC1"')
    print(device.compare_config())  # +snmp-server location "DC1"
    device.commit_config()  # applies the change and runs "save config"
```

### Optional arguments

| Argument              | Default                | Description |
|-----------------------|------------------------|-------------|
| `save_config`         | `True`                 | Run `save config` after a successful commit |
| `startup_config_file` | `/intflash/config.cfg` | File returned as `startup` by `get_config()` |
| `force_no_enable`     | `False`                | Do not enter privileged (`enable`) mode |
| `config_method`       | `cli`                  | How a merge is applied, see [Configuration management](#configuration-management) |
| `dest_file_system`    | `/intflash`            | Where `config_method="file"` stores its files |
| `merge_cfg`           | `napalm_merge.cfg`     | Candidate file name for `config_method="file"` |
| `rollback_cfg`        | `napalm_rollback.cfg`  | Restore point saved before each `config_method="file"` commit |

All Netmiko connection arguments (`port`, `secret`, `key_file`, `use_keys`,
`ssh_config_file`, `conn_timeout`, ...) are passed through. The driver
`timeout` (default 60 s) is also used as the read timeout of every command, so
raise it for very large configurations or MAC tables.

## Supported getters

| Method                       | Commands used |
|------------------------------|---------------|
| `get_facts`                  | `show sys-info`, `show interfaces gigabitEthernet`, `show running-config \| include domain-name` |
| `get_environment`            | `show sys-info`, `show khi performance cpu`, `show khi performance memory` |
| `get_interfaces`             | `show interfaces gigabitEthernet` (+ `... name` on releases without the Port Name table) |
| `get_interfaces_ip`          | `show ip interface vrfids 0-512`, `show ipv6 address interface` |
| `get_interfaces_counters`    | `show interfaces gigabitEthernet statistics [verbose]`, `... error` |
| `get_lldp_neighbors`         | `show lldp neighbor` |
| `get_lldp_neighbors_detail`  | `show lldp neighbor [port <port>]` |
| `get_arp_table`              | `show ip arp [vrf <name>]` |
| `get_mac_address_table`      | `show vlan mac-address-entry` |
| `get_vlans`                  | `show vlan basic`, `show vlan members` |
| `get_ntp_servers`            | `show running-config` |
| `get_snmp_information`       | `show sys-info`, `show running-config` |
| `get_config`                 | `show running-config [verbose]`, `more <startup file>` (banners and timestamps removed; `sanitized=True` masks keys, secrets and communities) |
| `cli`                        | any command; CLI errors are returned as output (like the IOS driver) |
| `is_alive`                   | |

Values the switch does not report are returned as `-1` (NAPALM convention),
for example `tx_errors`/`tx_discards` in `get_interfaces_counters`.

## Configuration management

Fabric Engine has no candidate datastore and no configuration replace, so the
driver supports **merge** operations:

| Method                   | Supported | Notes |
|--------------------------|-----------|-------|
| `load_merge_candidate`   | yes       | From `filename` or `config` |
| `compare_config`         | yes       | Incremental diff against the running config: `+line` will be added, `-line` will be removed (`no ...`), already configured lines are omitted |
| `commit_config`          | yes       | Applies the candidate, then `save config` (unless `save_config=False`). `message` and `revert_in` are not supported |
| `discard_config`         | yes       | |
| `load_replace_candidate` | no        | Raises `ReplaceConfigException` |
| `rollback`               | no        | Raises `NotImplementedError` |

Two ways to apply a merge (`config_method`):

* **`cli`** (default): the lines are sent in configuration mode. The first line
  the switch rejects stops the merge and raises `MergeConfigException`; lines
  before it stay applied.
* **`file`** (IOS-style): `load_merge_candidate` copies the candidate to the
  switch with SCP (verified with `file-checksum md5`). `commit_config` first
  saves the current configuration as a restore point
  (`save config file /intflash/napalm_rollback.cfg`) and then applies the file
  with `source`. Requires SCP (`boot config flags sshd`). The restore point is
  not applied automatically - Fabric Engine cannot replace the running config.

### File transfer

`napalm_extreme_fe.file_transfer.ExtremeFEFileTransfer` implements Netmiko's
`BaseFileTransfer` interface for Fabric Engine (SCP via the SSH server, `ls` for
existence and size checks, `file-checksum md5` to verify transfers). It is
written so that it can be contributed to Netmiko as-is.

## Reporting parsing problems

Please open an issue with the Fabric Engine release and the raw output of the
command that does not parse (the commands per getter are listed above). Remove
anything sensitive (hostnames, IP addresses, serial numbers, keys) first.

## Development

```bash
pip install -e ".[dev]"
pytest --cov                 # unit tests (mocked device output), coverage >= 85 %
ruff check . && ruff format --check .
mypy napalm_extreme_fe
```

Getter tests use the standard NAPALM test suite (`napalm.base.test.getters`)
with mocked device output in `test/unit/mocked_data/`, generated from the raw
captures in `test/unit/cli_samples/<case>/` (`normal/` holds hand-written
samples, the other directories real, anonymised captures):

```bash
python test/unit/build_mocked_data.py --expected   # then review every expected_result.json!
```

Releases: bump `version` in `pyproject.toml`, update `CHANGELOG.md`, push a tag
`v<version>`; the release workflow tests, builds and publishes to PyPI.

## License

Apache 2.0
