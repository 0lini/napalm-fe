# Agent guide: napalm-extreme-fe

NAPALM community driver for Extreme Networks Fabric Engine (formerly VOSS).
Read `README.md` first for the user-facing behaviour.

## Layout

| Path | What |
|------|------|
| `napalm_extreme_fe/extreme_fe.py` | The driver (`ExtremeFEDriver`): connection, getters, config management |
| `napalm_extreme_fe/parsers.py` | Pure text parsers, one per CLI output; no device access |
| `napalm_extreme_fe/file_transfer.py` | Netmiko `BaseFileTransfer` for Fabric Engine (SCP) |
| `test/unit/cli_samples/<case>/` | Raw CLI output, one file per command (`show sys-info` -> `show_sys-info.txt`, `|` -> `pipe`) |
| `test/unit/build_mocked_data.py` | `TESTS`: which commands each getter test needs; builds `mocked_data/` |
| `test/unit/mocked_data/` | Generated - never edit by hand |
| `tools/capture.py` | Capture all commands from a switch into a new case (read-only) |
| `tools/smoke_test.py` | Run all getters on a live switch; `--test-config` for a reversible config test |

## Commands

```bash
pip install -e ".[dev]"
pytest --cov                     # must stay green, coverage >= 85 %
ruff check . && ruff format --check .
mypy napalm_extreme_fe tools
python test/unit/build_mocked_data.py --expected   # after changing cli_samples or parsers
```

Run all of them before every commit, and only commit when they pass
(use `set -o pipefail` if you pipe pytest output).

## Conventions

- Follow NAPALM's IOS driver (`napalm/ios/ios.py`) for behaviour and naming
  where Fabric Engine allows it.
- Parsers live in `parsers.py`, take raw output and return plain data. Prefer
  parsing by table title / header (`iter_tables`, `header_columns`,
  `tables_by_title`) over fixed column positions: layouts differ between
  releases and `show` commands often print several tables.
- Every parser change needs a real capture in `cli_samples/` that covers it.
  `normal/` holds hand-written samples; all other case directories must be real
  device output (trimming rows is fine, inventing content is not).
- Values the switch does not report are `-1` / `-1.0` / `""`, never guessed.
- Do not name hardware models in code or docs; the driver targets the OS.
- Python 3.10+ (CI tests 3.10 - 3.15).

## Working with a test switch

1. **Capture** (read-only):

   ```bash
   python tools/capture.py --host <ip> --user <user> --case <model>_<release> \
       --anonymize --anonymize-ips [--vrf <vrf>] [--replace <name>=<placeholder> ...]
   ```

   Name the case after the platform and release, e.g. `VSP-4900_8.10.0.0`
   (case directory names are the one place a model name is fine).

2. **Review the capture for sensitive data** before anything else: hostnames of
   neighbours (LLDP, FDB tunnel names), domain names, public IPs, serials, keys,
   user names. Add `--replace` pairs and capture again, or edit the files.
   Never commit unreviewed captures. Never commit credentials.

3. **Build and compare**: `python test/unit/build_mocked_data.py --expected`,
   then check every new `expected_result.json` against the raw output. If a
   value is wrong, fix the parser (not the JSON) and rebuild.

4. **Live check**: `python tools/smoke_test.py --host <ip> --user <user>`.
   Getters that fail or return empty results where the switch has data are bugs.

5. NAPALM's own test suite can also run against the live switch:

   ```bash
   NAPALM_TEST_MOCK=0 NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> \
   NAPALM_PASSWORD=<pw> NAPALM_OPTIONAL_ARGS='{"port": 22}' \
   pytest test/unit/test_getters.py
   ```

   (Some getter tests require non-empty results, e.g. ARP; an empty table on the
   test switch is not a driver bug.)

### Safety rules for the switch

- `capture.py` and `smoke_test.py` (without `--test-config`) only send
  read-only commands. Do not run other configuration commands.
- `smoke_test.py --test-config` changes only `snmp-server location`, restores
  it, and never saves the config (`save_config=False`). Run it only on a lab
  switch. With `--config-method file` it leaves `napalm_merge.cfg` and
  `napalm_rollback.cfg` in `/intflash`.
- Never run `save config`, `boot`, `reset`, `delete`, `copy` or
  `source` by hand on the switch.

## Open items (verify on a real switch)

Confirmed on Fabric Engine 9.4: all getters except the ones below, `ls`,
`file-checksum md5`, `save config file`, `show running-config | include`, SCP.

1. **`config_method="file"` end to end**: what does `source <file>` print on
   success and on a rejected line? Does it need the `config terminal` / `end`
   wrapper the driver adds? Run `smoke_test.py --test-config --config-method file`
   and capture the raw `source` output into `cli_samples` + a unit test.
2. **Routed interfaces**: `show ip interface vrfids 0-512`,
   `show ipv6 address interface`, `show ip arp` and `show ip arp vrf <vrf>` have
   only been seen empty. Capture them from a switch with IP interfaces / ARP
   entries and check `parse_ip_interfaces`, `parse_ipv6_interfaces` (interface
   column is `VID/BID/TID`) and `parse_arp_table`.
3. **Older releases (VOSS 8.x) and VSP hardware**: check that `show sys-info`,
   `show interfaces gigabitEthernet` (does it include the Port Name / Port
   State tables?), `statistics verbose`, `show vlan basic` and
   `show khi performance cpu/memory` still parse; multi-slot chassis output
   for `get_environment` (CPU per slot, memory summed).
4. **SNMP communities**: only SNMPv3 has been seen; capture a switch with v1/v2c
   communities and check `parse_snmp_communities`.
5. **Enhanced secure mode** (`boot config flags enhancedsecure-mode true`):
   prompt / login behaviour has not been tested.

Not implemented yet (NotImplementedError, skipped in tests): BGP, users,
optics, route table, NTP stats/peers, network instances, ping/traceroute,
IPv6 neighbours. Implement them with real captures only.
