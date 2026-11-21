# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

First release (0.1.0). Requires Python 3.10+, NAPALM 5 and Netmiko 4.
Tested on Fabric Engine 9.1 - 9.4 (GNS3 VMs, see `docs/fe.rst`).

### Added

- NAPALM driver `fe` for Extreme Networks Fabric Engine (formerly VOSS):
  `get_network_driver("fe")`.
- Two transports, like NAPALM's EOS and NX-OS drivers: `ssh` (default, CLI over
  Netmiko, every release) and `https` (the switch's OpenAPI, Fabric Engine
  9.3.1 or later with a Premier license). With `https`, `get_facts`,
  `get_lldp_neighbors[_detail]`, `get_arp_table`, `get_mac_address_table`,
  `get_vlans`, `get_ntp_servers` and `get_snmp_information` read the API's
  JSON; everything else runs its CLI commands through the API. Like NX-OS, SSH
  is only opened for SCP uploads. `get_snmp_information` returns the community
  strings only over `https` (the CLI never shows them).
- Getters: `get_facts`, `get_environment`, `get_interfaces`,
  `get_interfaces_ip`, `get_interfaces_counters`, `get_lldp_neighbors`,
  `get_lldp_neighbors_detail`, `get_arp_table`, `get_mac_address_table`,
  `get_vlans`, `get_ntp_servers`, `get_snmp_information`, `get_users`
  (password only over `https`), `get_config` (with
  `sanitized`), `cli`, `is_alive`.
- Configuration management like the IOS driver: `load_merge_candidate`,
  incremental `compare_config`, `commit_config`, `discard_config`; the running
  configuration is saved to `rollback_cfg` before every commit.
  `config_method="file"`: SCP upload and `source <file> debug`.
- Replace and `rollback()`: the file is installed as the boot configuration and
  the switch is rebooted, like ExtremeCloud IQ Site Engine's configuration
  restore (requires `allow_reboot=True`).
- Commit confirm: `commit_config(revert_in=...)` schedules a reset that reboots
  into the saved configuration unless `confirm_commit()` cancels it;
  `has_pending_commit()` reads the switch log (requires `allow_reboot=True`).
- Configuration templates for `load_template()`: `set_hostname`,
  `set_ntp_servers`, `delete_ntp_servers`, `snmp_config`,
  `delete_snmp_config`, `set_users` / `delete_users` (create and delete
  users; over SSH by answering the prompts like ExtremeCloud IQ Site Engine,
  over `https` through `configuration/accounts`).
- `ExtremeVspFileTransfer`: Netmiko file transfer class (SCP) for device type
  `extreme_vsp`, written to be contributed to Netmiko.
- Log messages printed into the session (`logging screen`) are removed from all
  output.
- `docs/fe.rst`: caveats in the format of NAPALM's driver docs (prerequisites,
  transports, optional arguments, what needs SSH, tested releases).
