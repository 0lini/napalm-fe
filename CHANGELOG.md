# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

First release (0.1.0). Requires Python 3.10+, NAPALM 5 and Netmiko 4.
Tested on Fabric Engine 9.1 - 9.4 (GNS3 VMs, see `docs/fe.rst`).

### Added

- NAPALM driver `fe` for Extreme Networks Fabric Engine (formerly VOSS):
  `get_network_driver("fe")`. SSH (Netmiko `extreme_vsp`), like NAPALM's IOS
  driver.
- Getters: `get_facts`, `get_environment`, `get_interfaces`,
  `get_interfaces_ip`, `get_interfaces_counters`, `get_lldp_neighbors`,
  `get_lldp_neighbors_detail`, `get_arp_table`, `get_mac_address_table`,
  `get_vlans`, `get_ntp_servers`, `get_snmp_information`, `get_config`
  (with `sanitized`), `cli`, `is_alive`.
- Configuration merge like the IOS driver: `load_merge_candidate`,
  incremental `compare_config`, `commit_config` (saves with `save config`
  unless `save_config=False`), `discard_config`.
- Log messages printed into the session (`logging screen`) are removed from all
  output.
- `docs/fe.rst`: caveats in the format of NAPALM's driver docs.
