# Agent guide: napalm-fe

NAPALM community driver for Extreme Networks Fabric Engine (formerly VOSS).
Read `README.md` first for the user-facing behaviour.

## Branches

`main` is the core: SSH only (like NAPALM's IOS driver), the getters and the
merge workflow. Features are developed on their own branches, each `main` plus
exactly that feature (created by reverting its "Remove ..." commit on `main`);
merge one back when it is ready:

| Branch | Feature |
|--------|---------|
| `feature/openapi` | `transport="https"` (OpenAPI, Fabric Engine 9.3.1+, Premier license) |
| `feature/file-method` | `config_method="file"` (SCP upload + `source`), `ExtremeVspFileTransfer` for Netmiko |
| `feature/replace-rollback` | replace + `rollback()` (boot config + reboot, `allow_reboot`); needs the SCP upload, so it contains `feature/file-method` |
| `feature/commit-confirm` | `revert_in`, `confirm_commit`, `has_pending_commit` (`reset -in`, switch log) |
| `feature/templates` | `load_template`: `set_hostname`, NTP and SNMP templates |
| `feature/users` | `get_users`, `set_users` / `delete_users` (SSH; the OpenAPI variant via `configuration/accounts` is in `archive/full`) |
| `archive/full` | everything together (state of 2026-11-21) |

The findings below about these features (lab results, API behaviour, safety
rules) stay here so they are not lost; the code is on the branches. User docs
of a feature (README, `docs/fe.rst`, CHANGELOG) come back with its merge
(`archive/full` has them).

## Layout

| Path | What |
|------|------|
| `src/napalm_fe/fe.py` | The driver (`FEDriver`): connection, getters, config management |
| `src/napalm_fe/parsers.py` | Pure text parsers, one per CLI output; no device access |
| `test/unit/mocked_data/<test>/<case>/` | Getter test data: raw output per command + `expected_result.json` (same layout as NAPALM's IOS tests) |
| `test/unit/test_getters.py` | NAPALM's getter test suite, run for every case in `mocked_data` |
| `test/unit/test_*.py` | Unit tests for parsers and config management |
| `docs/fe.rst` | User docs / caveats in the format of NAPALM's `docs/support/<driver>.rst` (keep in sync with the code) |
| `test/unit/TestFEDriver.py` | Config tests against a live lab switch (not collected by default), fixtures in `test/unit/fe/` |

## Commands

```bash
pip install -e ".[dev]"
pytest --cov                     # must stay green, coverage >= 85 %
ruff check . && ruff format --check .
mypy src
```

Run all of them before every commit, and only commit when they pass
(use `set -o pipefail` if you pipe pytest output).

## Conventions

- Follow NAPALM's Cisco drivers for behaviour and naming where Fabric Engine
  allows it: IOS (`napalm/ios/ios.py`) for the CLI workflow and NX-OS
  (`napalm/nxos/nxos.py`, `nxos_ssh`) for the structure. Like NX-OS, all
  transport specific code is in a few primitives (`open`, `close`, `is_alive`,
  `_send_command`, `_send_config`, `_copy_run_start`; they reach SSH through
  `_ssh`), so that a second transport (`feature/openapi`) only implements
  those. A reference checkout of NAPALM may be in `napalm/` (ignored by git).
  Known deviations: a failed
  merge is not rolled back automatically (rollback reboots the switch). IOS and
  NX-OS roll back because their devices can do it without a reboot; community
  drivers for platforms that cannot (napalm-dellos10, napalm-netiron,
  napalm-aos) raise and leave it to the caller, and so do we, with
  napalm-dellos10's wording ("automatic rollback not available"). The
  merge diff is our own (`parsers.merge_diff`) because netutils'
  `diff_network_config` has no Fabric Engine / VOSS parser.
- Parsers live in `parsers.py`, take raw output and return plain data. Prefer
  parsing by table title / header (`iter_tables`, `header_columns`,
  `tables_by_title`) over fixed column positions: layouts differ between
  releases and `show` commands often print several tables.
- Every parser change needs real device output in `mocked_data/` (or inline in
  a unit test) that covers it. `synthetic` cases are hand-written; all other
  cases must be real device output (trimming rows is fine, inventing content
  is not).
- Values the switch does not report are `-1` / `-1.0` / `""`, never guessed.
- Do not name hardware models in code or docs; the driver targets the OS.
- Python 3.10+ (CI tests 3.10 - 3.15).
- `show running-config` (and so most config related commands) needs
  privileged mode; the driver enters it after login (`enable`), as Netmiko /
  the IOS driver do. `show logging file` also works in user mode.

## Working with a test switch

1. **Capture** (read-only): for each getter, run the commands listed in the
   README table and save the raw output into
   `test/unit/mocked_data/test_<getter>/<case>/<file>.txt`. `<file>` is the
   command with every character that is not a letter or digit replaced by `_`
   (NAPALM's `sanitize_text`): `show sys-info` -> `show_sys_info.txt`,
   `show running-config | include domain-name` ->
   `show_running_config___include_domain_name.txt`. Look at the existing
   `fabric_engine_9.4` cases for the exact set of files per test (the
   `test_get_config*` tests share the same files). Name the case after the
   software, not the hardware, e.g. `fabric_engine_9.3` or `fabric_engine_9.5_chassis`.

2. **Anonymise and review** before anything else: replace hostnames (own and
   LLDP/FDB tunnel neighbours), domain names, IP addresses (use 192.0.2.0/24,
   198.51.100.0/24, example.com), serial numbers, keys and user names
   consistently across all files of the case. Never commit unreviewed output.
   Never commit credentials.

3. **Expected results**: write `expected_result.json` for each new case. It
   may be generated by calling the getter on the patched driver from
   `test/unit/conftest.py`, but then check every value against the raw output
   by hand. If a value is wrong, fix the parser, not the JSON.

4. **Live getter check**: run NAPALM's getter test suite against the switch:

   ```bash
   NAPALM_TEST_MOCK=0 NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> \
   NAPALM_PASSWORD=<pw> NAPALM_OPTIONAL_ARGS='{"port": 22}' \
   pytest test/unit/test_getters.py
   ```

   Getters that fail, or return empty results where the switch has data, are
   bugs. (Some tests require non-empty results, e.g. ARP; an empty table on the
   test switch is not a driver bug.)

5. **Live config check** (lab switch only):

   ```bash
   NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> NAPALM_PASSWORD=<pw> \
   NAPALM_OPTIONAL_ARGS='{"port": 22}' pytest test/unit/TestFEDriver.py -v
   ```

   Runs merge, typo and discard tests. It only changes `snmp-server
   location`, restores it, and never saves the configuration.

### Safety rules for the switch

- Only send read-only commands (`show`, `dir`, `ls`, `more`, `file-checksum`), except
  through `TestFEDriver.py` (step 5) or when the user explicitly allows
  writing on a lab switch.
- Never send commands that ask for a password (`username <name> level <l>`,
  `cli password ...`) through the OpenAPI CLI operation: on 9.4 the main
  process `cbcp-main.x` crashed and the switch rebooted (core file in
  `/intflash/coreFiles/1/`, 2026-11-21). The CLI operation answers (y/n)
  questions itself, but not password prompts. The driver must never send
  such commands over `https`.
- Never ask for CLI help (`reset ?` etc.) over SSH: the line stays in the
  input buffer and a later newline runs it (9.4: `reset ?` became `reset`
  with its (y/n) question; the next command was taken as the answer).
- Never save the configuration (`save_config=False`) and never run
  `save config`, `boot`, `reset`, `delete`, `copy` or `source` by hand.
- `config_method="file"` leaves `napalm_merge.cfg` and `napalm_rollback.cfg`
  in `/intflash`.

## Open items (verify on a real switch)

Confirmed on Fabric Engine 9.4: all getters except the ones below,
`dir /intflash -l` (size, full path, `<DIR>`; free space in its last line),
`file-checksum md5`,
`save config file`, `show running-config | include`, SCP,
`terminal more disable` (sent by Netmiko at login; accepted without output).

VOSS / Fabric Engine 8.x is end of life (8.10 is the last 8.x release) and is
not supported; do not add test cases for it. VOSS 8.10.1.0 (GNS3) passes the
getter suite and `TestFEDriver.py` over SSH, but only with paramiko
3.x: it only offers the host keys `ssh-rsa` / `ssh-dss`.

Version matrix (GNS3 VMs, 2026-11-20; lab nodes fe810 / fe91 / fe92 / fe931,
see `~/vms/gns3/versions_boot.sh` and `versions_data.sh`): 9.1.0.0, 9.2.0.0,
9.3.0.0, 9.3.1.0 and 9.4.0.0 pass NAPALM's getter suite (53) and
`TestFEDriver.py` (6) over SSH. OpenAPI: 9.1 / 9.2 have no
`openapi local-mgmt` command (`% Invalid input`), 9.3.0.0 refuses it (license
of the image), 9.3.1.0 and 9.4.0.0 pass the getter suite and the config tests
over `https`.

Confirmed on the Fabric Engine 9.3 VM (GNS3 image of Extreme, reports as
`5520-24T-FabricEngine`): NAPALM's getter suite (all 39 with the test data of
the `fabric_engine_9.3` cases; on a fresh VM ARP, VRF `TEST` and NTP are
empty) and `TestFEDriver.py` (all 6).
Findings there:

- `save config file <file>` to an existing file asks
  `File [<file>] already existing, overwrite (y/n) ?`; anything but `y` prints
  `Save config to file <file> Aborted.` Saving takes a few seconds on the VM
  (`Saving Configuration...` first); meanwhile `show running-config` answers
  `Another show or save in progress.  Please try the command later.`
- SCP uploads write the file completely but the switch closes the channel
  without the final acknowledgement (scp library: `No response from server`,
  OpenSSH `scp -O`: `lost connection`), also when overwriting.
- The `rwa` password change forced at the first login is lost on reboot
  unless the configuration is saved (VM: `rwa` / `rwa` again).

1. **`config_method="file"` end to end**: settled on 9.3 - `source
   <file>` and `source <file> stop` print nothing, also for a rejected line;
   `source <file> debug` echoes every line after a prompt and prints the
   error (`% Invalid input detected at '^' marker.`), then runs the remaining
   lines; options cannot be combined. Runs fine from privileged EXEC mode with
   the `config terminal` / `end` wrapper. Still to check: 9.4 and hardware.
   `show running-config` during a running save is not detected as an error
   yet (see above).
2. **Routed interfaces**: settled for VLAN interfaces on the 9.3 VM
   (`fabric_engine_9.3` cases: VLAN 10 with IPv4/IPv6 in the GlobalRouter,
   VLAN 20 in VRF `TEST`, hosts on 1/2 and 1/3). `show ipv6 address interface`
   names the interface `V-<vid>` in its `VID/BID/TID` column and prints
   addresses uncompressed (`2001:db8:10:0:0:0:0:1`); `show ip interface`
   lists the out-of-band port as `mgmt-oob` (VRF MgmtRouter). The switch
   stores VRF names in lower case (`ip vrf TEST` -> `ip vrf test`, tables
   titled `VRF test`); `show ip arp vrf TEST` still works. Still to check:
   brouter ports and tunnels (`B-` / `T-` in `VID/BID/TID`, IPv4 name of a
   brouter port), hardware.
3. **Other 9.x releases and VSP hardware**: check that `show sys-info`,
   `show interfaces gigabitEthernet` (the driver reads its Port Name and Port
   State tables, seen on 9.3 / 9.4), `statistics verbose`, `show vlan basic` and
   `show khi performance cpu/memory` still parse; multi-slot chassis output
   for `get_environment` (CPU per slot, memory summed).
4. **SNMP communities**: settled - Fabric Engine never displays community
   strings (not in the running config, masked in `show snmp-server community`,
   per the VOSS 9.2 user guide stored encrypted in `snmp_comm_moc.txt`), so
   `community` is always empty and `get_snmp_information` does not read the
   running config. The OpenAPI (`configuration/snmp`) does return them in
   plain text (9.4), see "OpenAPI transport".
5. **Replace / rollback** (reboot): prompts confirmed on 9.4 (`copy` to an
   existing file asks `File [...] already existing, overwrite (y/n) ?`; the
   driver uses `reset -y`). Confirmed on the 9.4 VM (https): `rollback()`
   rebooted into the copied `rollback_cfg`. Only on a lab switch with console access.
6. **Commit confirm** (`revert_in`): confirmed on 9.4: `reset -in` syntax
   (`d:hh:mm | hh:mm | mm`), its prompt (`Are you sure you want to perform a
   scheduled reset? (y/n) ?`) and `reset -cancel` without a scheduled reset
   (`Warning! No ongoing scheduled reset!`), successful `reset -in` (no output,
   only a log message) and `reset -cancel` (`Scheduled reset successfully
   canceled!`). No `show` command displays a scheduled reset, so
   `has_pending_commit()` reads the log (`show logging file event-code`,
   oldest entry first): pending if the newest `0x0003068e` (enabled) is newer
   than the newest `0x0003068f` (canceled) and `0x00034594` (System boot).
   The switch writes these entries a few seconds after the command (through
   the API the driver is faster than that), so after `reset -in` / `reset
   -cancel` the driver waits until the log shows the change (`_wait_for_log`,
   at most `LOG_DELAY` seconds); without it `has_pending_commit()` right after
   a commit / confirm reported the old state (seen on 9.4, 2026-11-21).
   Confirmed on the 9.4 VM: an expired `reset -in` reboots the switch, and the
   log afterwards holds only the new `System boot` entry (the VM starts with an
   empty log), so nothing is reported pending. Still to check: that an unsaved
   change is really gone after the revert reboot, and the log on hardware
   (does it keep entries across a reboot?).
7. **Log messages in the session**: settled - `logging screen` is a setting
   of the CLI login session, like Cisco's `terminal monitor`, although it is
   entered in config mode. Verified on 9.1.0.0, 9.2.0.0, 9.3.0.0, 9.3.1.0 and
   9.4.0.0 (GNS3 VMs, three of them factory default; 2026-11-22), with the
   log entries of a second SSH login / logout as test messages
   (`show logging config` shows the session's own value):
   - serial console: `screen : on` by default, gets every message (also at
     the `Login:` prompt); after `no logging screen` none
   - SSH: every new session starts with `screen : off` and gets none; after
     `logging screen` in that session it gets them (whole lines between
     commands), other sessions still show `off`
   - not stored: `show running-config` / `verbose` do not change, so `save
     config` cannot keep it; after logout and login the console is back to
     `on`
   The driver never sends `logging screen`, so it never sees log messages;
   the former filter (`LOG_MESSAGE_RE`, with an exception for `show logging`)
   was removed. Earlier notes that messages appear in SSH sessions came from
   the serial console or a session that had enabled it. Still to check:
   hardware, Telnet sessions.
8. **Enhanced secure mode** (`boot config flags enhancedsecure-mode true`):
   prompt / login behaviour has not been tested.

Configuration templates (branch `feature/templates`; `src/napalm_fe/templates/*.j2`, `load_template()`),
verified on 9.4 for both transports (2026-11-21): `sys name X` sets the
system name, but the running config shows `prompt "X"` (also settable, `no
prompt` resets it), so `set_hostname` uses `prompt`; a changed prompt is
handled by `set_base_prompt()` after a commit. SNMPv1/v2c groups: `readgrp`
read-only, `v1v2grp` read-write; `snmp-server community <c> group <g> index
<i> secname <s>`; `no snmp-server community <community string>` (not the
index). Open: over `https`, `get_snmp_information` lists every community once
per context (`c`, `c::1`, `c::512` - VRF contexts), as the API returns it.

Users (branch `feature/users`; 2026-11-21, 9.4; `show cli username` also on 9.3): `show cli
username` lists all accounts (UserName, AccessLevel, State, Type; header
between two `===` rules); the API `configuration/accounts` the same plus the
encrypted secret and `removable`; the running config shows none. Six default
accounts (`ro`, `l1`, `l2`, `l3`, `rw`, `rwa`), up to seven more with
`username add <name> level <ro|rw|rwa> enable` (asks the password twice,
interactively). `username <name> level <l>` (without `add`) renames the
default account of that level after `Do you want to change username for the
default RO user ? (y/n) ?`. `no username <name> enable` disables, `no
username <name>` deletes after `The specified user name will be deleted!
Continue (y/n) ?`. No `-y` / password argument (command reference 8.10 -
9.2, Extreme KB 000081060); XIQ-SE's scripts answer the prompts in the CLI
session. So `set_users` / `delete_users` are not configuration lines:
`load_template` keeps them as `user_changes`, `commit_config` applies them -
ssh: `_apply_user_change_ssh` answers the prompts (the password is only sent
after `Enter password`), https: `POST configuration/accounts` (levels only
READ_ONLY / ADMIN_LIMITED / ADMIN: 422 otherwise; 422 if the user exists or
the password contains the name) and `DELETE configuration/accounts/{name}`
(422 for default accounts). Passwords change only with the old one: CLI `cli
password <user> <read-only|layer1|layer2|layer3|read-write|read-write-all>`
(asks old, new, new - also for rwa changing another user), API `PATCH
configuration/accounts/{name}` with `level`, `secret`, `oldSecret` (`level`
must be the current one: "Incorrect level for this username"). The schema's
`superUserName` / `superUserPassword` variant is refused with correct rwa
credentials ("Invalid username or password."), other spellings are not
accepted; maybe only for enhanced secure mode (not checked). `set_users` takes
`old_password` for that. Verified on 9.4 for both
transports. Still to check: whether new users survive a reboot without `save
config`.

Enhanced secure mode: `boot config flags enhancedsecure-mode non-jitc` asks
`Changing enhancedsecure-mode flag will result in deleting all sensitive
files, are you sure you want to continue? (y/n) ?` (9.4) before `save config`
and a reboot; then `admin` / `admin` with a forced complex password, roles
admin / privilege / operator / security / auditor, users with `password
create-user <role> <name>`. Not tested yet (lab node `fe94esm`).

Not implemented yet (NotImplementedError, skipped in tests): BGP, users,
optics, route table, NTP stats/peers, network instances, ping/traceroute,
IPv6 neighbours. Implement them with real captures only.

## OpenAPI transport (`transport="https"`, branch `feature/openapi`)

One driver with a `transport` option like EOS / NX-OS; `ssh` (default) is
the CLI over Netmiko, `https` the switch's OpenAPI. History (never
released): `transport="openapi"` with `openapi_port` / `verify`; then
`https` as the default; back to `ssh` as the default on 2026-11-20 after the
version matrix showed that OpenAPI only exists from 9.3.1 (with a Premier
license). Without OpenAPI the login error says what is needed.

Verified on the Fabric Engine 9.4.0.0 VM (GNS3 image FEGNS3.9.4.0.0, OpenAPI
server 0.1.13.12, schema "Extreme NOS API" 0.2.0, OpenAPI 3.0.3):

- needs a Premier (or Pilot / Platform ONE) license; the 9.4 VM has one. The
  9.3.0.0 GNS3 image refuses it (`Software License Violation` in the log, the
  setting is not applied); 9.3.1.0 should work (Extreme community)
- enabled with `application` / `openapi local-mgmt enable`; the server then
  listens on TCP 9443, TLS 1.2, self-signed certificate. After enabling, it
  takes up to ~3 minutes until `show application openapi` shows
  `Server State Local Port : Enabled` (connection refused until then)
- token: `POST /rest/openapi/v0/operation/auth-token/:generate` with
  `{"username": ..., "password": ...}` -> `200 {"token": "...", "ttl": 86400}`;
  sent as header `X-Auth-Token` (the schema also allows a cookie
  `x-auth-token`). Valid 24 h unless the server restarts
  (`TTL(in seconds) : 86400` in `show application openapi`). What
  `.../auth-token/:disable` and `:enable` do is undocumented (see the
  design: not used). The schema declares HTTP basic auth as
  well, but the switch rejects it (401) - a token is required
- concurrency limits (`/v0/state/capabilities/openapi`): 7 requests at a time
  in total, 3 per client
- schema: `GET /rest/openapi/v0/state/openapi/schema` (YAML, 1.4 MB, 358
  paths); `/rest/openapi/openapi.json` exists but answers 404 "not available
  in this OS version". `GET /v0/state/capabilities/urls` lists what this
  switch supports (348 URLs, with request / response attribute types); the
  schema's extra paths (PoE, certificates) are missing on the VM
- trimmed copies in `test/unit/openapi/`: `schema.yaml` (the paths of the
  coverage map below and the components they use) and
  `supported_operations.txt` (method + path of every supported URL)
- errors: `{"errorCode": 404, "errorMessage": ..., "resource": ...,
  "debugLog": ...}` - `debugLog` contains the decoded token (user, source IP,
  expiry): never log or store error bodies unfiltered
- `configuration/snmp` returns the SNMPv1/v2c community strings in plain text
  (the CLI never shows them): API responses are secrets, anonymise them
- port names are `1:1` in the API (`localName` / CLI: `1/1`); speeds are
  strings (`"1000M"`, `"0M"`); `sysUpTime` is in 1/100 s
- CPU, memory, temperatures and interface counters are only in the telemetry
  the switch pushes (`subscription/stats`, `...CallbackElement` schemas), not
  in any GET endpoint
- `POST /v0/operation/system/cli` runs a list of CLI commands
  (`["show ...", ...]` -> `{"data": [{"cliInput", "cliOutput",
  "statusCode": 200|400|409}], "metadata": {"succeeded", "failed",
  "skipped"}}`, HTTP 200 or 207). Each request starts in privileged EXEC
  mode (`configure terminal` / `end` per request). Without
  `continue_on_failure` the commands after a failed one come back with
  `statusCode` 409 (skipped). Pipes (`| include`) are rejected (`% Invalid
  input`). `source <file> debug` stops at the first rejected line (over SSH
  it goes on). Errors may start with a BEL (`\x07Error: The specified VLAN
  ID is invalid or already in use`)
- `POST /v0/operation/system/config/:save` with `{}` (boot config) or
  `{"name": <path>}`: 204, overwrites an existing file without asking; 422
  with a message for a bad path
- the CLI operation confirms (y/n) questions itself: `copy` onto an existing
  file overwrites it and only echoes `File [...] already existing,` (an
  extra `y` command is `% Invalid input`), `delete` and `reset -in <time>`
  print nothing, `reset -cancel` prints `Scheduled reset successfully
  canceled!` / `Warning! No ongoing scheduled reset!`, `reset -y` reboots
  (the request may not return). Over SSH `copy` / `delete` ask and have no
  documented option to skip it (`copy ... -y` is accepted through the API)
- `POST /v0/operation/system/reboot/:schedule` `{"delay": <minutes>}` (not
  seconds: 300 logs "Unit will reset in 0 days 5 hours 0 minutes!"); only
  10 - 360 accepted (422 "Delay must be between 10 and 360."), although the
  schema allows up to 1728000; not used (`reset -in` through the CLI
  operation has the full range). Logs the same event codes as `reset -in`,
  a few seconds later. `:cancel` (no body) answers 204 also when nothing is
  scheduled and cancels a `reset -in` too
- `PATCH /v0/configuration/system/save-config` `{"primary": ..., "backup":
  ...}` sets the boot configuration files (not used); `state/system` has
  `isConfigDirty`. No generic file transfer: `image/:install` only installs
  images from a URL, `debug-info/:upload` only sends files away;
  `/v0|v1|v2/configuration` are GET only
- `operation/system/config/recovery` (`:timeout`, `:cancel`, `undo-list`)
  undoes changes made through the `configuration/*` endpoints, not CLI lines
- the full schema and captures are in the lab directory (`~/vms/gns3/openapi`)

Design and state (verified on the 9.4 VM, 2026-11-20):

- `openapi.py`: an HTTP session (`requests`) that gets the token on `open()`
  and renews it once on a 401; one request at a time (the switch allows 3
  per client). `close()` does not end the token: `auth-token/:disable` and
  `:enable` have no description and may switch token authentication off for
  the whole switch, so they are never called. Tokens and the `debugLog` of
  error bodies never reach logs or exceptions. `ssl_verify=False` disables
  urllib3's InsecureRequestWarning (like NAPALM's NX-API client)
- getters whose data the API has completely read its JSON (`get_facts`,
  `get_lldp_neighbors[_detail]`, `get_arp_table`, `get_mac_address_table`,
  `get_vlans`, `get_ntp_servers`, `get_snmp_information`); all other getters
  and `cli()` run their CLI commands through the CLI operation
  (`_send_command`, same output as SSH without the `Command Execution Time`
  banner; `get_interfaces` then reads `show clock`). A getter's result always
  comes from one source. `get_interfaces_ip` uses the CLI: `configuration/vlan`
  lists other addresses (link-local of the onboarding VLAN, VLAN 4092 instead
  of `mgmt-oob`). Live on 9.4 all getters equal SSH except live values and
  the documented differences (VLANs 4092 / 4094, the cloud agent's NTP
  server, SNMP communities); no SSH connection is opened
- configuration over the API (all live-tested on 9.4): merge lines
  (`_send_config_api`, `continue_on_failure=false`), `save config` /
  `rollback_cfg` (`:save`), `source` (`config_method="file"`), `copy`,
  `delete`, `reset -in` / `-cancel` / `-y` (`_send_command_timing`),
  `dir` and the log. `rollback()` over https rebooted into `rollback_cfg`
- SSH (port 22, like NX-OS `ensure_netmiko_conn`): only SCP uploads. In the
  GNS3 lab SSH is forwarded to another port, so uploads cannot be tested
  with `https` there (set `driver.netmiko_optional_args["port"]` by hand in
  a lab script)
- tests: mocked JSON responses per getter in `mocked_data/<test>/<case>_openapi/`
  (real responses only, anonymised), `FakeOpenApiSession` in `conftest.py`,
  the getter suite runs for both transports

Coverage map (step 2; from the schema and real responses of the 9.4 VM -
LLDP and ARP were empty there, their field names are from the schema only):

| NAPALM method | OpenAPI (GET unless noted) | Not in the API (CLI) |
|---------------|----------------------------|----------------------|
| `get_facts` | `state/system` (`sysName`, `cards[].modelName` / `serialNumber` / `firmwareVersion`, `sysUpTime` / 100), `configuration/dns` (`domains`), `v1/state/ports` (`localName`) | - |
| `get_environment` | `state/system/fans` (`operStatus`), `state/system/power-supplies` (`operStatus`, `capacity`, `outputPower` - more than the CLI) | temperature, CPU, memory |
| `get_interfaces` | `v1/state/ports` (`operState`, `operSpeed`, `macAddress`), `configuration/ports` (`enabled`, `description`) | MTU, `last_flapped` |
| `get_interfaces_ip` | `configuration/vlan` (`addressList` per VLAN interface, `vrName`), `configuration/mgmt-interface`, `configuration/loopback` | - |
| `get_interfaces_counters` | - | all counters |
| `get_lldp_neighbors[_detail]` | `state/lldp` (`neighbors[]`: `portName`, `sysName`, `portId`, `portDesc`, `chassisId`, `sysDesc`, `sysCapSupported` / `sysCapEnabled`), `state/lldp/ports/{port}` | - |
| `get_arp_table` | `state/forwarding/ip-arp?vr_name=` (`arpList[]`, paginated `start` / `count`) | - |
| `get_mac_address_table` | `state/forwarding/mac-fdb` (`status`, `interfaceName`, `vlanId`) | - |
| `get_vlans` | `configuration/vlan` (`name`), `v1/state/vlan` (`memberInterfaces`) | - |
| `get_ntp_servers` | `configuration/ntp` (`vrBasedSettings[].servers`; also lists the server the cloud agent sets, which the running config does not show) | - |
| `get_snmp_information` | `configuration/snmp` (`contact`, `location`, `v1v2.access[]` with `community` and `accessView`) | - |
| `get_config` | - (`/v0/configuration` is JSON, not the CLI text NAPALM returns) | running / startup config |
| `cli` | POST `operation/system/cli` | - |
| config management | POST `operation/system/cli` (merge lines, `source`, `copy`, `delete`, `reset`), `operation/system/config/:save`; `configuration/*` PATCH / PUT exist, but no candidate / diff / commit and no file transfer | file upload (SCP) |
| new getters (not implemented today) | `state/forwarding/ipv6-nd` (`get_ipv6_neighbors_table`), `state/transceiver/{port}` (`get_optics`), `configuration/accounts` (`get_users`), `state/route` (`get_route_to`) | - |

Safety: against the switch only `GET` requests, the token request
(`auth-token/:generate`; never `:disable` / `:enable`, see the design) and -
for tests - `show` commands through POST `operation/system/cli`, unless
writing on a lab switch is allowed (then also see the safety rules above:
a saved configuration must be removed again, the lab expects none); never
store tokens or passwords in the repository.
