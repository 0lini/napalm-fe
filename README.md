# napalm-fe

[NAPALM](https://github.com/napalm-automation/napalm) community driver for
**Extreme Networks Fabric Engine** (formerly VOSS, the VSP Operating System
Software). It works with any switch running Fabric Engine / VOSS; the CLI is the
same across hardware platforms, so there is no model-specific code.

The driver only uses the switch's API (OpenAPI over HTTPS); it never opens an
SSH connection. Getters read the API's JSON where it has the data, everything
else (and all configuration) goes through the API's CLI operation. The API needs
Fabric Engine 9.4.2 or later (no license needed) or 9.3.1 / 9.4.0 with a Premier
license, and `openapi local-mgmt enable`. Details in
[docs/fe.rst](docs/fe.rst#minimum-version).

> **Status: beta.** The getters are tested against real device output (see
> `test/unit/mocked_data/`). Output formats can differ between releases - if
> something does not parse on your switch, please open an issue with the raw
> CLI output (see [Reporting parsing problems](#reporting-parsing-problems)).

Caveats and the optional arguments are in [docs/fe.rst](docs/fe.rst) (same
format as NAPALM's driver caveats). Features in development (replace / rollback,
commit confirm, templates, user management) live on `feature/*` branches.

## Installation

```bash
pip install napalm-fe
```

Requires Python 3.10+ and NAPALM 5.

## Usage

```python
from napalm import get_network_driver

driver = get_network_driver("fe")
with driver("192.0.2.10", "admin", "secret", optional_args={"ssl_verify": False}) as device:
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
| `port`                | `9443`                 | Port of the API |
| `ssl_verify`          | `False`                | Check the API's TLS certificate (`True` or a CA bundle path) |

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
  output; `synthetic` cases are hand-written. Getters that read the API's JSON have
  the (anonymised) responses in the case as `<path>.json`.
- `test/unit/test_parsers.py` and `test_config.py` are unit tests for parsers
  and configuration management.
- `test/unit/TestFEDriver.py` runs configuration tests against a live
  lab switch (like `TestIOSDriver.py`); it is not part of the normal test run:

  ```bash
  NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> NAPALM_PASSWORD=<password> \
  NAPALM_OPTIONAL_ARGS='{"ssl_verify": false}' pytest test/unit/TestFEDriver.py
  ```

Releases: bump `version` in `pyproject.toml`, update `CHANGELOG.md`, push a tag
`v<version>`; the release workflow tests, builds and publishes to PyPI.

## License

Apache 2.0
