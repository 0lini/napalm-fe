# napalm-extreme-fe

[NAPALM](https://github.com/napalm-automation/napalm) community driver for
**Extreme Networks Fabric Engine** (formerly VOSS, the VSP Operating System
Software). It works with any switch running Fabric Engine / VOSS; the CLI is the
same across hardware platforms, so there is no model-specific code.

The driver connects over SSH with [Netmiko](https://github.com/ktbyers/netmiko)
and parses the CLI output. Netmiko still names the platform `extreme_vsp`; the
driver automatically uses the newest name Netmiko provides (`extreme_fe`, then
`extreme_voss`, then `extreme_vsp`).

> **Status: beta.** The getters are tested against real device output (see
> `test/unit/mocked_data/`). Output formats can differ between releases - if
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
| `allow_reboot`        | `False`                | Allow replace commits and `rollback()`, which reboot the switch |
| `dest_file_system`    | `/intflash`            | Where the driver stores its files on the switch |
| `candidate_cfg`       | `napalm_candidate.cfg` | Replace candidate |
| `merge_cfg`           | `napalm_merge.cfg`     | Merge candidate for `config_method="file"` |
| `rollback_cfg`        | `napalm_rollback.cfg`  | Running configuration saved before every commit |

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

The workflow follows NAPALM's IOS driver. Where Fabric Engine has no
equivalent of an IOS feature (`configure replace`), the driver does what
ExtremeCloud IQ Site Engine's configuration restore does: install the file as
the boot configuration and reboot the switch.

| Method                   | Notes |
|--------------------------|-------|
| `load_merge_candidate`   | From `filename` or `config` |
| `load_replace_candidate` | Copies the full configuration to `candidate_cfg` on the switch (SCP) |
| `compare_config`         | Merge: incremental diff against the running config (`+line` added, `-line` removed by `no ...`, configured lines omitted). Replace: `-`/`+` for every differing line |
| `commit_config`          | Saves the running config to `rollback_cfg` first (like IOS), then applies the candidate, see below. `revert_in` see [Commit confirm](#commit-confirm-revert_in); `message` is not supported |
| `confirm_commit`         | Cancels the scheduled revert reset and saves the configuration |
| `has_pending_commit`     | Whether a `revert_in` commit waits for `confirm_commit()` (tracked by the driver instance) |
| `discard_config`         | Drops the candidate |
| `rollback`               | Installs `rollback_cfg` as the boot configuration and reboots; while a `revert_in` commit is pending it reverts that commit now (`reset -y`). Needs `allow_reboot=True` |

**Merge** (`config_method`), saved with `save config` afterwards unless
`save_config=False`:

* **`cli`** (default): the lines are sent in configuration mode. The first line
  the switch rejects stops the merge and raises `MergeConfigException`; lines
  before it stay applied.
* **`file`** (IOS-style): `load_merge_candidate` copies the candidate to the
  switch with SCP (verified with `file-checksum md5`); `commit_config` applies
  it with `source`. Requires SCP (`boot config flags sshd`).

**Replace and rollback** reboot the switch and therefore require
`allow_reboot=True`; otherwise they raise `ReplaceConfigException`. The file is
copied over the boot configuration (`startup_config_file`, default
`/intflash/config.cfg`) and the switch is reset. The driver closes the
connection; call `open()` again once the switch is back.

### Commit confirm (`revert_in`)

Like ExtremeCloud IQ Site Engine, `commit_config(revert_in=<seconds>)` protects
a merge against changes that make the switch unreachable:

1. the current configuration is saved (`save config`)
2. a reset is scheduled with `reset -in` (rounded down to whole minutes, like
   IOS; 1 minute to 9 days 23:59)
3. the change is applied **without** saving

`confirm_commit()` cancels the reset (`reset -cancel`) and saves. If it is not
called in time, the switch reboots into the configuration saved in step 1.
Requires `allow_reboot=True`. Note that step 1 also saves earlier unsaved
changes.

```python
device.load_merge_candidate(config="vlan create 100 type port-mstprstp 0")
device.commit_config(revert_in=300)  # reboots into the old config in 5 minutes ...
device.confirm_commit()  # ... unless confirmed
```

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
mypy src
```

Tests are organised like NAPALM's IOS driver tests:

- `test/unit/test_getters.py` runs NAPALM's getter test suite against mocked
  device output in `test/unit/mocked_data/<test>/<case>/` (one file per command
  plus `expected_result.json`). `fabric_engine_9.4` cases are real, anonymised
  output; `synthetic` cases are hand-written.
- `test/unit/test_parsers.py`, `test_config.py` and `test_file_transfer.py`
  are unit tests for parsers, configuration management and file transfer.
- `test/unit/TestExtremeFEDriver.py` runs configuration tests against a live
  lab switch (like `TestIOSDriver.py`); it is not part of the normal test run:

  ```bash
  NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> NAPALM_PASSWORD=<password> \
  pytest test/unit/TestExtremeFEDriver.py
  ```

Releases: bump `version` in `pyproject.toml`, update `CHANGELOG.md`, push a tag
`v<version>`; the release workflow tests, builds and publishes to PyPI.

## License

Apache 2.0
