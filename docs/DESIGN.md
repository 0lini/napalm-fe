# Design: napalm-fe

The driver talks to the switch's OpenAPI only (no SSH) and is used both as a
plain NAPALM driver and by the NetBox Diode agent (orb-agent, device-discovery
backend) to fill NetBox with as much native data as possible.

This document is the target design. Where the code differs today, the
migration plan at the end says how to get there. `AGENTS.md` has the lab
findings and safety rules; this file has the structure.

## Principles

1. **As simple as the problem allows.** NAPALM drivers are usually one big file
   plus helpers (NX-OS and EOS are about 2,200 lines each; the Diode agent's
   bundled drivers share private helper modules such as `_modules.py`). Add
   structure when it starts to hurt, not before.
2. **Pure conversion, thin I/O.** A converter takes JSON (or CLI text) and
   returns NAPALM data. It does no requests and never raises on a missing
   field. This is what makes real-capture fixtures enough to test everything.
3. **One source per getter.** A getter's result comes from one place (the API
   JSON, or the CLI operation where the API has no data), never a mix.
4. **Unreported means unreported.** Missing values are `-1`, `-1.0` or `""`,
   never guessed.
5. **Read-only discovery.** Getters never change or save the configuration.
6. **No hardware model names** in code or docs. Classify by the API's own
   fields (`type`, `connectorType`, ...), not by part-number strings.

## Layout

```
src/napalm_fe/
  fe.py              FEDriver: open/close/is_alive; every getter is a short
                     method that calls its domain module
  api/
    session.py       HTTPS session: token, renew on 401, error filtering
                     (today: openapi.py)
    cli.py           CLI operation: run(), run_config(), save_config()
    resources.py     one constant per endpoint path; get_all() handles
                     start/count paging
    snapshot.py      per-cycle request cache (see below)
  names.py           the only place that converts port names ("1:1" <-> "1/1")
                     and builds VLAN / LAG interface names
  domains/           one module per data domain: fetch(api) + pure convert()
    facts  interfaces  ip  vlans  switchport  lag  vrf
    lldp  arp  mac  ntp  snmp  modules  environment
  cli_text/          CLI text parsers, only for what the API cannot give
    sys_info  khi  counters  config_text   (today: parsers.py)
  config/            merge candidate, diff (merge_diff), save, error detection
  util.py            safe lookups (get(d, "a", "b", default)), sentinels
```

`openapi_parsers.py` becomes the `convert()` half of the domain modules;
`parsers.py` becomes `cli_text/`. Nothing else changes in behaviour.

## A getter, end to end

```python
# fe.py
def get_facts(self):
    return facts.get_facts(self._snapshot)


# domains/facts.py
def get_facts(snap):
    return convert(
        snap.get(res.STATE_SYSTEM), snap.get(res.CONFIG_DNS), snap.get(res.STATE_PORTS_V1)
    )


def convert(system, dns, ports):  # pure: tested with captured JSON
    ...
```

- `snap.get(path)` returns the cached JSON for the current cycle.
- Optional getters (`get_modules`, `get_interfaces_lag`, ...) exist only when
  implemented. The Diode agent looks them up with `getattr` and skips missing
  ones; a failing one costs only its own data.

## Request cache (`snapshot`)

Facts, interfaces, switchport and modules all read `state/system` and
`v1/state/ports`. The snapshot fetches each path once per cycle.

- It lives for one `open()` .. `close()`; `open()` always starts empty, so
  nothing is stale across discovery runs.
- It caches GET responses only. CLI commands are never cached.
- It needs its own test (second call does not hit the API; `open()` clears it).

The switch allows 3 concurrent requests per client, so requests stay
sequential. Batching with `POST /v0/operation/bulk` is a later optimisation,
only if a real discovery run is measurably slow.

## Session and authentication

- Token from `POST /v0/operation/auth-token/:generate`, sent as
  `X-Auth-Token`, renewed once on a 401. Never call `:disable` / `:enable`.
- **Request a short `ttl`** (schema allows 60..86400 s; option `token_ttl`,
  default about 15 minutes). The driver cannot log out, so every run would
  otherwise leave a 24-hour token behind, which adds up under a polling agent.
- **Check the release in `open()`**, like EOS (`show version`) and NX-OS
  (`show hostname`) do: one cheap GET (`state/system`), and a clear
  `ConnectionException` if the endpoint is missing. This catches release
  differences (9.4.2 is untested) at the first request.
- Errors map to NAPALM exceptions at the boundary: connection and login
  problems to `ConnectionException`, rejected requests and bad replies to
  `CommandErrorException`. Tokens and the `debugLvl` / `debugLog` of error
  bodies never reach logs or exceptions.
- Options: `port` (9443), `ssl_verify` (False, like NX-OS), `timeout`,
  `token_ttl`, `save_config`, `startup_config_file`.

## NetBox (Diode) data

What the agent reads (device-discovery 1.35.0; read from its source, not
run):

| NetBox data | Getter | Required | API source |
|-------------|--------|----------|------------|
| Device: name, model, vendor, serial, platform | `get_facts` | yes | `state/system`, `configuration/dns` |
| Interfaces: name, enabled, MAC, description, speed (Mbps), MTU | `get_interfaces` | yes | `v1/state/ports`, `configuration/ports` |
| IP addresses, derived prefixes | `get_interfaces_ip` | yes | `configuration/vlan`, `loopback`, `mgmt-interface` |
| VLANs | `get_vlans` | VLAN option | `configuration/vlan`, `v1/state/vlan` |
| Port mode, untagged / tagged VLANs | `get_interfaces_vlans` | no | `configuration/vlan/ports` |
| VLAN id of routed interfaces | `get_interfaces_vlan_id` | no | `configuration/vlan` |
| LAG membership (`lag` on member ports) | `get_interfaces_lag` | no | `configuration/lag` |
| VRFs | `get_network_instances` | `discover_vrfs` | `configuration/vrf` |
| Modules, VIMs, optics | `get_modules` | `discover_modules` | `state/system`, `state/transceiver/{port}` |
| Stack members | `get_chassis_members` | no | `state/system` `cards[]` |

- The agent guesses the NetBox **interface type** from the interface name and
  speed, so names and speeds must be exact. LAG interfaces must be named so the
  agent types them `lag`.
- The agent does not use LLDP, so no cables come from this path. The LLDP
  getters stay for plain NAPALM use.
- Site, role, tenant and rack come from the agent policy, not the driver.
- The driver returns plain dicts shaped like the agent's envelopes. It does not
  import the agent's package. A contract test validates the shapes against a
  small vendored schema, so an agent change shows up as one failing test.

### Modules and optics (Cisco `ios` driver is the template)

`get_modules()` returns `{"members": {None: {"bays": [...],
"interfaces_by_bay": {...}}}}` (member id instead of `None` for stacks), or
`None` when it finds neither a slot bay nor an optic. Each bay is
`ModuleBay(name, position, ModuleEntry(model, serial, type, description,
identified, manufacturer, sub_bays))`.

- `type` is `linecard`, `supervisor`, `fan`, `psu` or `transceiver`. PSUs and
  fans are classified but never emitted.
- A **VIM** is a pluggable port module of a fixed-form switch. It maps to
  `linecard`, like Cisco's "FRU uplink module". An empty slot is not emitted.
- **Optics** on fixed-port switches are device-rooted bays named after their
  interface. Emit one only when it is a transceiver with a non-empty model and
  serial. A serial without a part number is recorded with `identified=False`,
  the description as model, and manufacturer `Unknown`.
- Model is the part number, manufacturer the optic's own vendor name
  (`vendorName`), serial `vendorSn`. Only ask ports whose connector is a
  transceiver and that are populated.
- `interfaces_by_bay` links each interface to its module. Its names must equal
  the `get_interfaces` keys exactly.
- Slot modules, VIM positions and stacks need a real capture from such a
  switch before they are implemented; the 9.4 VM has one card and no optics.

## Testing

- `FakeApi` replays captured JSON by path (as `FakeOpenApiSession` does
  today); fixtures live in `test/unit/mocked_data/<test>/<case>/` with
  `expected_result.json`, and NAPALM's getter suite runs over every case.
- Real captures only (trimming rows is fine, inventing content is not);
  anonymise hostnames, IPs, MACs, serials and keys across a whole case.
  `synthetic` cases are hand-written and say so.
- One generic test checks every getter's return type against NAPALM's models.
- Contract tests for the Diode envelopes (see above).
- A capture script (planned) records read-only GETs and `show` commands and
  runs the anonymiser, so a new case is one command plus a review.
- Live checks as described in `AGENTS.md` (getter suite with
  `NAPALM_TEST_MOCK=0`, then `TestFEDriver.py` on a lab switch only).

## Adding a feature

1. Add the endpoint constant to `api/resources.py`.
2. Capture real output, anonymise it, add the case under `mocked_data/`.
3. Write `domains/<x>.py` (`get_...` plus a pure `convert`), test the
   converter against the case.
4. Add the one-line getter to `fe.py`.
5. Add its row to the "Data per getter" table in `docs/fe.rst`.

## Migration plan

1. **Refactor, no behaviour change.** Move code into the layout above in small
   commits with `pytest --cov`, `ruff` and `mypy` green after each.
2. **Session:** `token_ttl` and the `open()` version check; fix paging
   (`get_all`). The empty ARP table in the API (CLI has entries) is the known
   case; compare `vr_name` and `count` on `state/forwarding/ip-arp` first.
3. **Core getters from the API:** interfaces with MTU and speed, IP addresses
   with prefix lengths and VRF.
4. **Diode getters, one commit each:** `get_interfaces_vlans`,
   `get_interfaces_lag`, `get_network_instances`, `get_interfaces_vlan_id`,
   then `get_modules` (optics first), then `get_chassis_members` if stacks are
   in use.
5. Update `AGENTS.md` (layout, recipe) and `docs/fe.rst` as each lands.

## Open items

- All API shapes come from 9.4.0.0 captures and the schema; 9.4.2 is
  unverified, including the claim that the API needs no license there.
- Return formats of `get_interfaces_lag` and `get_interfaces_vlan_id` are
  assumed from the agent's code; confirm in its `lag.py` and `runner.py`
  before implementing.
- How `vims[]` and `cards[]` look on modular or stacked hardware.
- How the agent loads our package: it walks installed `napalm_*`
  distributions, so a pip-installed `napalm-fe` should appear as driver `fe`
  (read from its source, not tried).
- Whether the switch limits the number of live tokens.
