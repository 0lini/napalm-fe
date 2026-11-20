# napalm-extreme-fe

[NAPALM](https://github.com/napalm-automation/napalm) community driver for
**Extreme Networks Fabric Engine** (formerly VOSS, the VSP Operating System
Software). It works with any switch running Fabric Engine / VOSS; the CLI is the
same across hardware platforms, so there is no model-specific code.

Like NAPALM's NX-OS driver, it talks to the switch's API (OpenAPI over HTTPS,
`transport="https"`, the default) and opens an SSH connection
([Netmiko](https://github.com/ktbyers/netmiko) `extreme_vsp`) only for what the
API cannot do: file transfers (SCP) and commands that ask for confirmation or
reboot the switch. `transport="ssh"` uses only SSH and works without the
OpenAPI license.

> **Status: beta.** The getters are tested against real device output (see
> `test/unit/mocked_data/`). Output formats can differ between releases - if
> something does not parse on your switch, please open an issue with the raw
> CLI output (see [Reporting parsing problems](#reporting-parsing-problems)).

Caveats, the optional arguments and what uses the API or SSH are in
[docs/extreme_fe.rst](docs/extreme_fe.rst) (same format as NAPALM's driver
caveats).

## Installation

```bash
pip install napalm-extreme-fe
```

Requires Python 3.10+, NAPALM 5 and Netmiko 4.

The `https` transport needs the OpenAPI on the switch (Premier license):

```
configure terminal
application
openapi local-mgmt enable
```

## Usage

```python
from napalm import get_network_driver

driver = get_network_driver("extreme_fe")
with driver("192.0.2.10", "admin", "secret") as device:  # https, port 9443
    print(device.get_facts())
    print(device.get_lldp_neighbors())

    device.load_merge_candidate(config='snmp-server location "DC1"')
    print(device.compare_config())  # +snmp-server location "DC1"
    device.commit_config()  # applies the change and runs "save config"

# CLI over SSH only
with driver("192.0.2.10", "admin", "secret", optional_args={"transport": "ssh"}) as device:
    print(device.get_facts())
```

Optional arguments (details in [docs/extreme_fe.rst](docs/extreme_fe.rst#optional-arguments)):

| Argument              | Default                | Description |
|-----------------------|------------------------|-------------|
| `transport`           | `https`                | `https` (OpenAPI) or `ssh` (CLI), like NAPALM's EOS / NX-OS drivers |
| `port`                | `9443` / `22`          | Port of the transport. With `https`, SSH (when needed) uses port 22, like NX-OS |
| `ssl_verify`          | `False`                | Check the API's TLS certificate (`True` or a CA bundle path), like NX-OS |
| `save_config`         | `True`                 | Run `save config` after a successful commit |
| `startup_config_file` | `/intflash/config.cfg` | File returned as `startup` by `get_config()` |
| `force_no_enable`     | `False`                | SSH: do not enter privileged mode |
| `config_method`       | `cli`                  | Merge with configuration lines (`cli`) or SCP + `source` (`file`) |
| `allow_reboot`        | `False`                | Allow replace, `rollback()` and `revert_in`, which reboot the switch |
| `dest_file_system`, `candidate_cfg`, `merge_cfg`, `rollback_cfg` | `/intflash`, `napalm_*.cfg` | Files of the driver on the switch |

All Netmiko connection arguments are passed to the SSH connection.

## Support matrix

Getters: `get_facts`, `get_environment`, `get_interfaces`, `get_interfaces_ip`,
`get_interfaces_counters`, `get_lldp_neighbors`, `get_lldp_neighbors_detail`,
`get_arp_table`, `get_mac_address_table`, `get_vlans`, `get_ntp_servers`,
`get_snmp_information`, `get_config` (with `sanitized`), `cli`, `is_alive`.
Not implemented yet: BGP, users, optics, route table, NTP stats/peers, network
instances, ping/traceroute, IPv6 neighbours.

Configuration (in the format of NAPALM's configuration support matrix):

| _                     | extreme_fe |
|-----------------------|------------|
| **Config. replace**   | Yes [1] |
| **Config. merge**     | Yes |
| **Commit Confirm**    | Yes [1] |
| **Compare config**    | Yes [2] |
| **Atomic Changes**    | No |
| **Rollback**          | Yes [1] |

1. Emulated with a reboot (Fabric Engine has no `configure replace`), like
   ExtremeCloud IQ Site Engine; requires `allow_reboot=True`.
2. Hand-crafted by the driver: Fabric Engine has no on-box diff.

See [docs/extreme_fe.rst](docs/extreme_fe.rst) for the commands and API
endpoints per method and what still needs SSH.

### File transfer

`napalm_extreme_fe.file_transfer.ExtremeFeFileTransfer` implements Netmiko's
`BaseFileTransfer` interface for Fabric Engine (SCP via the SSH server,
`dir <directory> -l` for existence, size and free space, `file-checksum md5` to
verify transfers). It is written so that it can be contributed to Netmiko as-is.

## Reporting parsing problems

Please open an issue with the Fabric Engine release and the raw output of the
command that does not parse (the commands per getter are listed in
[docs/extreme_fe.rst](docs/extreme_fe.rst#where-the-data-comes-from)). Remove
anything sensitive (hostnames, IP addresses, serial numbers, keys) first.

## Development

```bash
pip install -e ".[dev]"
pytest --cov                 # unit tests (mocked device output), coverage >= 85 %
ruff check . && ruff format --check .
mypy src
```

Tests are organised like NAPALM's IOS driver tests:

- `test/unit/test_getters.py` runs NAPALM's getter test suite for both
  transports against mocked device output in
  `test/unit/mocked_data/<test>/<case>/` (one file per command or API request
  plus `expected_result.json`; `*_openapi` cases are for `https`).
  `fabric_engine_*` cases are real, anonymised output; `synthetic` cases are
  hand-written.
- `test/unit/test_parsers.py`, `test_config.py` and `test_file_transfer.py`
  are unit tests for parsers, configuration management and file transfer.
- `test/unit/TestExtremeFeDriver.py` runs configuration tests against a live
  lab switch (like `TestIOSDriver.py`); it is not part of the normal test run:

  ```bash
  NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> NAPALM_PASSWORD=<password> \
  NAPALM_OPTIONAL_ARGS='{"ssl_verify": false}' pytest test/unit/TestExtremeFeDriver.py
  ```

Releases: bump `version` in `pyproject.toml`, update `CHANGELOG.md`, push a tag
`v<version>`; the release workflow tests, builds and publishes to PyPI.

## License

Apache 2.0
