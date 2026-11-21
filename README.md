# napalm-fe

[NAPALM](https://github.com/napalm-automation/napalm) community driver for
**Extreme Networks Fabric Engine** (formerly VOSS, the VSP Operating System
Software). It works with any switch running Fabric Engine / VOSS; the CLI is the
same across hardware platforms, so there is no model-specific code.

Like NAPALM's IOS driver, it connects over SSH
([Netmiko](https://github.com/ktbyers/netmiko) `extreme_vsp`) and parses the
CLI output. Tested (GNS3 VMs): Fabric Engine 9.1, 9.2, 9.3, 9.3.1 and 9.4;
VOSS 8.10 works with paramiko 3.x. Details in
[docs/fe.rst](docs/fe.rst#minimum-version).

> **Status: beta.** The getters are tested against real device output (see
> `test/unit/mocked_data/`). Output formats can differ between releases - if
> something does not parse on your switch, please open an issue with the raw
> CLI output (see [Reporting parsing problems](#reporting-parsing-problems)).

Caveats and the optional arguments are in [docs/fe.rst](docs/fe.rst) (same
format as NAPALM's driver caveats). Features in development (OpenAPI transport,
replace / rollback, commit confirm, SCP merge, templates, user management) live
on `feature/*` branches.

## Installation

```bash
pip install napalm-fe
```

Requires Python 3.10+, NAPALM 5 and Netmiko 4.

## Usage

```python
from napalm import get_network_driver

driver = get_network_driver("fe")
with driver("192.0.2.10", "admin", "secret", optional_args={"port": 22}) as device:
    print(device.get_facts())
    print(device.get_lldp_neighbors())

    device.load_merge_candidate(config='snmp-server location "DC1"')
    print(device.compare_config())  # +snmp-server location "DC1"
    device.commit_config()  # applies the change and runs "save config"
```

Optional arguments (details in [docs/fe.rst](docs/fe.rst#optional-arguments)):

| Argument              | Default                | Description |
|-----------------------|------------------------|-------------|
| `save_config`         | `True`                 | Run `save config` after a successful commit |
| `startup_config_file` | `/intflash/config.cfg` | File returned as `startup` by `get_config()` |
| `force_no_enable`     | `False`                | Do not enter privileged mode |

All Netmiko connection arguments (`port`, `secret`, `key_file`, ...) are passed
through.

## Support matrix

Getters: `get_facts`, `get_environment`, `get_interfaces`, `get_interfaces_ip`,
`get_interfaces_counters`, `get_lldp_neighbors`, `get_lldp_neighbors_detail`,
`get_arp_table`, `get_mac_address_table`, `get_vlans`, `get_ntp_servers`,
`get_snmp_information`, `get_config` (with `sanitized`), `cli`, `is_alive`.

Configuration (in the format of NAPALM's configuration support matrix):

| _                     | fe |
|-----------------------|----|
| **Config. replace**   | No |
| **Config. merge**     | Yes |
| **Commit Confirm**    | No |
| **Compare config**    | Yes [1] |
| **Atomic Changes**    | No |
| **Rollback**          | No |

1. Hand-crafted by the driver: Fabric Engine has no on-box diff.

## Reporting parsing problems

Please open an issue with the Fabric Engine release and the raw output of the
command that does not parse (the commands per getter are listed in
[docs/fe.rst](docs/fe.rst#commands-per-getter)). Remove anything sensitive
(hostnames, IP addresses, serial numbers, keys) first.

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
  plus `expected_result.json`). `fabric_engine_*` cases are real, anonymised
  output; `synthetic` cases are hand-written.
- `test/unit/test_parsers.py` and `test_config.py` are unit tests for parsers
  and configuration management.
- `test/unit/TestFEDriver.py` runs configuration tests against a live
  lab switch (like `TestIOSDriver.py`); it is not part of the normal test run:

  ```bash
  NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> NAPALM_PASSWORD=<password> \
  NAPALM_OPTIONAL_ARGS='{"port": 22}' pytest test/unit/TestFEDriver.py
  ```

Releases: bump `version` in `pyproject.toml`, update `CHANGELOG.md`, push a tag
`v<version>`; the release workflow tests, builds and publishes to PyPI.

## License

Apache 2.0
