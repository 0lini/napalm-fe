# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- CI: test on Python 3.15; update GitHub Actions (checkout v7,
  setup-python v7, upload-artifact v7, download-artifact v8).

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
