# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `transport` option (like NAPALM's EOS driver): `ssh` (default) or
  `openapi`. With `openapi`, `get_facts`, `get_lldp_neighbors[_detail]`,
  `get_arp_table`, `get_mac_address_table`, `get_vlans`, `get_ntp_servers`
  and `get_snmp_information` use the switch's OpenAPI (token login, options
  `openapi_port` and `verify`); the other getters and `cli()` run their CLI
  commands through the API, so reading never needs SSH. Configuration
  management uses SSH, opened on first use. Over the API,
  `get_snmp_information` returns the community strings.
- `discard_config()` deletes an uploaded replace candidate on the switch
  (like NAPALM's NX-OS drivers).

- `load_replace_candidate`, replace `commit_config` and `rollback()`: the file
  is installed as the boot configuration and the switch is rebooted, like
  ExtremeCloud IQ Site Engine's configuration restore. Requires
  `allow_reboot=True`.
- Commit confirm like ExtremeCloud IQ Site Engine: `commit_config(revert_in=...)`
  schedules a reset (`reset -in`) that reboots into the saved configuration
  unless `confirm_commit()` cancels it; `has_pending_commit()`. Requires
  `allow_reboot=True`.
- Log messages printed into the session (`logging screen`) are removed from all
  output.

### Changed

- Classes renamed to `ExtremeFeDriver` and `ExtremeFeFileTransfer`; the old
  names `ExtremeFEDriver` and `ExtremeFEFileTransfer` still work (aliases).
- `get_snmp_information` no longer reads the running configuration (Fabric
  Engine never shows community strings; `community` was always empty).
- `get_interfaces_counters` always reads `statistics`, `statistics verbose` and
  `error`.
- A merge in `config_method="cli"` also stops at `% Cannot ...` errors.

### Removed

- Fallbacks for output never seen on Fabric Engine 9.x: `show interfaces
  gigabitEthernet name`, the full running config when `| include` fails,
  alternative counter column names, a second MAC table layout, `ntp server
  address`, a version without parentheses in SysDescr.
- Handling of output only seen on the serial console (`reset -in` countdown,
  older log format).

- Like the IOS driver, the running configuration is saved to `rollback_cfg`
  before every commit (not only with `config_method="file"`).
- CI: test on Python 3.15; update GitHub Actions (checkout v7,
  setup-python v7, upload-artifact v7, download-artifact v8).
- `config_method="file"` runs the candidate with `source <file> debug`: without
  `debug` the switch prints nothing for a rejected line, so the merge seemed to
  succeed.

### Fixed

- `get_interfaces_ip` listed the IPv6 addresses of a VLAN interface under a
  separate `V-<vid>` interface and uncompressed (`2001:db8:10:0:0:0:0:1`);
  they are now merged into `Vlan<vid>` with the IPv4 addresses and compressed.

- Every commit after the first one failed: `save config file` asks before
  overwriting the existing `rollback_cfg` (`... already existing, overwrite
  (y/n) ?`); the driver now answers it and waits for the save to finish.
- `ExtremeFeFileTransfer` uploads failed with `No response from server`: the
  switch writes the file but closes the SCP channel without the final
  acknowledgement (Fabric Engine 9.3). This error is ignored, the upload
  is verified by size and MD5 as before.

## [0.1.0]

First release. Requires Python 3.10+, NAPALM 5 and Netmiko 4.

### Added

- Getters: `get_facts`, `get_environment`, `get_interfaces`,
  `get_interfaces_ip`, `get_interfaces_counters`, `get_lldp_neighbors`,
  `get_lldp_neighbors_detail`, `get_arp_table`, `get_mac_address_table`,
  `get_vlans`, `get_ntp_servers`, `get_snmp_information`, `get_config`
  (with sanitizing), `cli`, `is_alive`.
- Merge configuration management: `load_merge_candidate`, incremental
  `compare_config`, `commit_config`, `discard_config`.
- `config_method="file"`: SCP upload, restore point and `source` (IOS-style).
- `ExtremeFEFileTransfer`: Netmiko file transfer class for Fabric Engine.
