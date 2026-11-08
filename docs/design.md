# Design — CS2 Server Blocker for Linux

**Status:** implemented in v0.1.0; this document is kept as the design reference.
**Scope:** a firewall rule manager only. It does not read, modify or inject into the
game, its files, its memory or Steam. Native and Flatpak Steam are equivalent because
blocking happens in the host network stack.

---

## 1. Architecture

```
┌───────────────────────┐   read-only files    ┌──────────────────────────────┐
│ GUI (GTK4, user)      │◀────────────────────▶│ /etc/cs2blocker/config.json  │
│ CLI (cs2blockctl)     │                      │ /var/lib/cs2blocker/…        │
│ no root, no netfilter │                      │ /run/cs2blocker/…            │
└──────────┬────────────┘                      └──────────────▲───────────────┘
           │ pkexec + polkit action (auth_admin_keep)          │ atomic writes
           │ or sudo fallback                                  │
           ▼                                                   │
┌──────────────────────────────────────────────────────────────┴───────────────┐
│ cs2blocker-helper (root, short-lived, flock-serialized)                      │
│  parse POP codes/mode/preset → load root-owned SDR cache → validate again    │
│  → generate ruleset → nft -c -f - → nft -f - (one transaction)              │
└──────────┬───────────────────────────────────┬───────────────────────────────┘
           │ nft (primary)                     │ iptables + ipset (fallback only)
           ▼                                   ▼
    inet cs2blocker                    filter/CS2BLOCKER chain
    output (+ optional forward)        OUTPUT jump + ipsets
           │
           ▼
    kernel netfilter — the only thing that touches game traffic
```

A separate root `cs2blocker-monitor.service` reads our table's named counters every ~2 s
and atomically publishes `/run/cs2blocker/status.json` (0644). That lets the GUI show
live drops without a password prompt every 2 s. It can be dropped if you prefer counters
only after an authenticated Apply.

---

## 2. Trust and privilege boundaries

| component | runs as | may do | may not do |
|---|---|---|---|
| GUI / CLI | user | read config/cache/status, ping, spawn `pkexec` for Apply | write root files, run nft |
| helper | root | fetch/cache SDR, write config/state, apply/clear rules, enable units | accept raw IPs, paths, or nft text from users |
| monitor | root | read our counters, write `/run/.../status.json` | modify rules, fetch data |
| systemd | root | run helper `apply` / `refresh` | anything else |

**Helper input is deliberately tiny:** `apply --mode block|only --pops <codes>`,
`apply --preset <name>`, `refresh`, `clear`, `persistence enable|disable`, `monitor`,
`status`, `doctor`. No IP, CIDR, file path, nft snippet or shell string is accepted.

**Root-owned data.** The helper fetches HTTPS from exactly
`api.steampowered.com` (redirects allowlisted to the same host), 15 s timeout, 4 MiB cap,
full schema validation. It writes `/var/lib/cs2blocker/sdr-730.json` atomically only
after the whole document validates; a failed fetch leaves the last good cache untouched.
A root-installed bundled snapshot under `/usr/lib/cs2blocker/cs2blocker/data/` is used only when no
valid cache exists. The helper never reads SDR data from a user-writable path.

`/etc/cs2blocker/config.json` is root-owned 0644 and written only by the helper. At read
time the helper checks owner, regular-file type and that the file is not group/other
writable; otherwise it refuses. Writes use `O_NOFOLLOW`, `O_EXCL` temp files in the
target directory, `fsync`, `os.replace`, then directory `fsync`.

**Validation is repeated on every cache load**, not just after a fetch:

- `success is True`, non-empty `pops`, ≤200 POPs, ≤64 relays/POP, ≤5000 relays total;
- POP code `^[a-z0-9]{2,12}$`; relay is exactly one of `ipv4`/`ipv6`;
- address parses and is global unicast (reject private, loopback, link-local, multicast,
  unspecified, reserved, CGNAT `100.64.0.0/10`);
- `port_range` is `[start, end]`, `1 <= start <= end <= 65535`, span ≤ 1024;
- duplicate IPs inside a POP are removed; cross-POP duplicates are recorded for overlap
  handling;
- names/comments are sanitized to a small printable set and capped before entering nft
  comments; `revision` must be an integer; `fetched_at` is set locally.

**Elevation.** One polkit action `org.cs2blocker.helper` with
`<allow_active>auth_admin_keep</allow_active>` and an
`org.freedesktop.policykit.exec.path` annotation pointing at the installed helper. The
GUI calls `pkexec` only for Apply / Unblock / persistence changes. If no polkit agent is
running, the GUI shows the exact `sudo cs2blockctl …` fallback; the CLI prints the
equivalent helper command. The helper also works as root under systemd/sudo with no
polkit at all.

**Hygiene.** `umask 022`; fixed `PATH=/usr/sbin:/usr/bin:/sbin:/bin`; absolute tool
paths; stripped `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONHOME`, `BASH_ENV`,
`ENV`, `IFS`; no `shell=True`; `flock` on `/run/cs2blocker/lock` serializes
apply/refresh/clear. Ruleset text goes to `nft` on stdin, never through a user file.

---

## 3. nftables backend (primary)

### 3.1 Ruleset shape

```nft
# Generated by cs2blocker. Apply with cs2blockctl apply.
destroy table inet cs2blocker          # nft >= 1.0.7; conditional delete otherwise
table inet cs2blocker {
  set sdr_ports {
    type inet_service
    flags interval
    comment "CS2 SDR relay UDP ports (derived from Valve data)"
    elements = { 27015-27140 }
  }
  counter cnt_ams { comment "cs2blocker drops for ams" }
  set pop_ams {
    type ipv4_addr
    flags interval
    comment "ams Amsterdam (Netherlands) [Europe]"
    elements = {
      155.133.248.36 comment "ams relay udp 27015-27060",
      155.133.248.37 comment "ams relay udp 27015-27060"
    }
  }
  chain output {
    type filter hook output priority filter; policy accept;
    udp dport @sdr_ports ip daddr @pop_ams \
      counter name cnt_ams drop comment "cs2blocker: drop ams relay UDP"
  }
  # only when block_forward = true:
  chain forward {
    type filter hook forward priority filter; policy accept;
    udp dport @sdr_ports ip daddr @pop_ams \
      counter name cnt_ams drop comment "cs2blocker: drop ams relay UDP (forward)"
  }
}
```

- Only `inet cs2blocker` is ever deleted/created. No `flush ruleset`, no
  `/etc/nftables.conf`, no other table, no `conntrack -F`.
- One named set per POP per family (IPv6: `pop_ams_v6`), one named counter per POP, one
  drop rule per set per chain. POPs with no relays get no set/counter/rule.
- `sdr_ports` is derived from the current document's port ranges (today 27015–27140).
- `flags interval` tolerates future CIDRs/ranges; element comments survive.
- `policy accept` plus only our matches means other firewalls keep working. An `accept`
  in another base chain does not override a later `drop`; our drop wins for the match.
- Optional `forward` catches CS2 in a VM/container; off by default.

### 3.2 Atomic apply

1. Build the complete table text in memory.
2. `nft -c -f -` (check; no state change).
3. `nft -f -` (one transaction: delete/destroy + recreate only our table).
4. On check or apply failure the previous table remains; save `state.json` only after
   success.

`destroy table` is idempotent when the table is absent. On nft < 1.0.7 (Debian 12,
Ubuntu 22.04), the helper checks table existence and emits `delete table` only when
needed; delete+create stay in one transaction. Capability is detected from
`nft --version`.

### 3.3 Modes

`known` = validated POPs; `selected` = explicit config POP codes.

| mode | blocked |
|---|---|
| `block` | `selected ∩ known` |
| `only` | `known − selected`, minus any IP also belonging to a selected POP |

- In `only` mode a newly appearing POP is blocked by default after refresh.
- Overlap handling: an IP shared between selected and non-selected POPs (none today) is
  excluded from the blocked set in `only` mode, and reported by `doctor`.
- Empty `block` selection clears the table. Empty `only` selection blocks every known
  POP; the GUI requires confirmation and the CLI warns.
- Presets are stored in `config.json` (source of truth) and editable through the
  helper. The package ships `data/presets.toml` as defaults, read with `tomllib`; the
  helper seeds them into a new config. Presets expand to explicit POP codes at apply
  time, so a new EU POP is not silently allowed by "EU only" until the preset is applied
  again.

### 3.4 Protocol and counters

Default `protocol = "udp"` (SDR relay traffic is UDP; ICMP stays available for the ping
column; less collateral on shared Valve IPs). `protocol = "all"` is an advanced option
that drops the `udp dport` match.

Named counters appear in `nft -j list table inet cs2blocker` with `packets`/`bytes`.
The monitor publishes them; `cs2blockctl counters [--watch]` reads the last snapshot.
Counters reset on every apply; the GUI labels them as drops since the last apply.

---

## 4. iptables/ipset fallback (only when nft is absent)

iptables has no custom tables, so the least invasive equivalent is the dedicated chain
below. This is the only place the design touches a shared chain, and it happens only
when nft is unavailable:

- dedicated chain `CS2BLOCKER` in `filter`; one jump `-I OUTPUT 1 -j CS2BLOCKER`
  (and `FORWARD` only if `block_forward`), added only if absent;
- one ipset per POP/family: `cs2b_<code>` (`hash:net family inet`),
  `cs2b6_<code>` (`inet6`);
- one rule per POP/family matching UDP SDR ports + destination set, `-j DROP`, with
  comment and per-rule counters;
- update by building `CS2BLOCKER_NEW` + `cs2b_*_new`, filling, swapping the
  jump/references, then deleting the old chain/sets. Best-effort atomic, not as strong
  as nft; failure leaves the old chain.

`ipset` is required; if missing, the helper reports the distro package name. `ufw reload`
or `firewalld --reload` may flush `filter` and remove the jump; `doctor` detects it and
`apply` restores it. This is why nft is primary: other managers do not touch
`inet cs2blocker`.

Auto-detect: `nft` present and `nft list tables` succeeds → nft. Else `iptables` +
`ipset` → fallback. Else fail with a package hint. Config can force `nft`/`iptables`.

---

## 5. Persistence

`cs2blocker.service` — oneshot, `RemainAfterExit=yes`:

```ini
[Unit]
Description=CS2 Server Blocker firewall rules
Wants=network-pre.target
After=network-pre.target nftables.service ufw.service firewalld.service
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/lib/cs2blocker/cs2blocker-helper apply
StateDirectory=cs2blocker
RuntimeDirectory=cs2blocker
ConfigurationDirectory=cs2blocker
# NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp,
# CapabilityBoundingSet=CAP_NET_ADMIN, ReadWritePaths for the three dirs

[Install]
WantedBy=multi-user.target
```

`cs2blocker-refresh.service`: oneshot, `After=network-online.target`,
`Wants=network-online.target`, `ExecStart=… refresh --timer`.
`cs2blocker-refresh.timer`: `OnCalendar=weekly`, `OnBootSec=15min`,
`RandomizedDelaySec=6h`, `Persistent=true`.
`cs2blocker-monitor.service`: simple, `ExecStart=… monitor`, `Restart=on-failure`,
pulled in by `cs2blocker.service`; exits when the table is gone.

`/etc/nftables.conf` and ufw/firewalld normally load before `network-pre.target`; our
service runs after them, so their boot-time flushes cannot delete our table. A manual
`nft -f /etc/nftables.conf` later can; `doctor` detects the missing table and `apply`
restores it.

At boot `apply` needs no GUI, session, D-Bus or network. It reads the root config, uses
the cache (or bundled snapshot on first run), validates, generates, checks, applies. If
the cache is stale, the old rules still apply and `state.json` records it.

`refresh` fetches, validates, atomically replaces the cache, then re-applies. On
failure it keeps cache and rules, records the error and exits 0 (a Valve hiccup should
not make the weekly timer red); `doctor`/GUI surface the stale state.

Non-systemd (Artix/OpenRC, Void/runit, Alpine/OpenRC): run the same `apply` from the
local init after the network/firewall service; `persistence`/`monitor` degrade
gracefully. README will carry one example per init.

Removal: `sudo cs2blockctl clear` deletes only our table, stops the monitor and empties
the saved selection. `sudo ./uninstall.sh` disables/removes units, polkit policy, desktop
file, icon, `/usr/lib/cs2blocker`, and optionally `/etc/cs2blocker` +
`/var/lib/cs2blocker`; it deletes only our table, never flushes.

---

## 6. File layout

Core data model: `SdrSnapshot(revision, fetched_at, source, pops)`,
`Pop(code, name, region, relays, geo, tier, aliases)`,
`Relay(address, port_start, port_end)`,
`Selection(mode, pops, block_forward, protocol)`, and
`Config(selection, presets, backend, notify_on_change)`.

```
repo/
  cs2blocker/
    core/{model,regions,sdr,ruleset,state,util}.py
    core/backends/{base,nft,iptables}.py
    data/{sdr-730.json,presets.toml}
    helper.py  cli.py  gui/
  packaging/{PKGBUILD,install.sh,uninstall.sh,polkit/,systemd/}
  tests/{fixtures/,test_*.py}
  docs/{phase0-research.md,design.md}
  research/           # Phase 0 evidence, not installed
  README.md

installed/
  /usr/lib/cs2blocker/{cs2blocker/,cs2blocker-helper,cs2blocker/data/}
  /usr/bin/{cs2blockctl,cs2blocker-gui}
  /usr/lib/systemd/system/{cs2blocker.service,cs2blocker-refresh.service,
                           cs2blocker-refresh.timer,cs2blocker-monitor.service}
  /usr/share/polkit-1/actions/org.cs2blocker.policy
  /usr/share/applications/cs2blocker.desktop
  /usr/share/icons/hicolor/scalable/apps/cs2blocker.svg
  /etc/cs2blocker/config.json          # runtime, 0644, helper-only writes
  /var/lib/cs2blocker/{sdr-730.json,state.json}
  /run/cs2blocker/{status.json,lock}
```

Packages install but **enable nothing**. The first Apply or an explicit
`systemctl enable` turns on boot persistence.

---

## 7. GUI and CLI

**GUI: GTK4 + libadwaita via PyGObject.** It is already installed here (GTK 4.24,
libadwaita 1.10, PyGObject 3.58), is native on GNOME, Wayland-native, and needs no Qt
platform-theme plugins. Trade-off: on KDE it uses Adwaita, not Breeze.

The primary view is a locally drawn equirectangular world map (Cairo, Natural Earth
1:110m public-domain land rings). Each POP with relay endpoints is a clickable dot.
Nearby dots cluster into a counted bubble; clicking it opens the POP list instead of
zooming. Blocked POPs are hollow rings with a slash, allowed POPs are solid, mixed
clusters are amber and pending POPs get dashed rings. Scroll zooms, drag pans, region
chips toggle a whole region, and the inspector shows city/country, status, latency and
drops, with relays and ports behind Details. No online tiles are used.

The GUI uses explicit Allowed/Blocked state per datacenter (checked means allowed).
The "Block newly added datacenters automatically" setting maps to the underlying
allowlist/blocklist policy without exposing the old mode toggle. Preset menu, Apply,
Allow all / Block all, and "Unblock all" remain. A floating pending bar appears over
the map with Revert and Apply. A status pill holds rules/backend/cache/persistence
details; boot persistence and manual refresh live in the Settings menu. Apply is the only elevation
point. Errors from `pkexec`/helper are shown verbatim, including the exact `sudo`
fallback. A relay-list change produces an in-app toast; selection warnings are shown
below the map.

**CLI:**

| command | root? | behavior |
|---|---|---|
| `list [--region R] [--json]` | no | POPs, regions, relays, ports, cache age |
| `status [--json]` | no | rules, selection, cache, persistence, last error |
| `block <pop…>` | yes | add, mode=block, apply |
| `unblock <pop…>` | yes | remove, apply |
| `only <pop…>` | yes | mode=only, exactly these, apply |
| `preset <name>` / `preset list|save|delete` | mixed | apply / manage presets |
| `refresh` | yes | fetch + cache + re-apply |
| `apply` | yes | re-apply saved selection |
| `clear` | yes | delete our table, empty selection, stop monitor |
| `ping [pop…]` | no | unprivileged ICMP |
| `counters [--watch]` | no | last published per-POP counters |
| `doctor [--json]` | mixed | see below |

Root commands re-exec through `pkexec` (or `sudo` with `--sudo`, or automatically when
no polkit agent is found). The CLI reads the same world-readable files as the GUI and
never writes them.

`doctor`: nft presence/version/kernel, our table present, backend/ipset availability,
pkexec/polkitd/agent, unit enabled/active, ufw/firewalld/Docker presence, whether
`/etc/nftables.conf` has `flush ruleset` + `nftables.service` enabled, cache
age/source/revision/error, config ownership/mode, IP overlaps, "blocks everything"
warnings.

---

## 8. Failure handling

| failure | behavior |
|---|---|
| fetch timeout / 500 / bad JSON | keep last good cache; record error; exit 0 |
| invalid IP/port/POP in document | reject whole document; keep cache; name the field |
| `nft -c` or `nft -f` fails | previous table untouched; show nft stderr |
| no nft and no iptables/ipset | clear error + distro package hint; no changes |
| no polkit agent | show exact `sudo` command; no silent failure |
| no systemd | apply/clear work; persistence toggle explains; monitor optional |
| config missing | empty selection, no rules; defaults written on first apply |
| cache missing, bundled exists | use bundled, mark `source: bundled`, warn |
| both missing | refuse to apply; clear error |
| table deleted by another tool | monitor inactive; `doctor` detects; `apply` restores |
| everything selected in `only` | warn; zero drop rules; matchmaking may fail |
| nothing selected in `only` | GUI confirmation; blocks all known POPs; CLI warns |
| ufw reload wipes iptables fallback | `doctor` detects; `apply` restores; nft unaffected |

Every change is reversible: `apply` and `clear` are idempotent; `uninstall.sh` removes
everything; no action touches another firewall table.

---

## 9. Testing, packaging, decisions

**Tests.** Unit: schema/validation rejections, region map, ruleset snapshots (`block
ams,fra`; `only ams,fra`; overlap; synthetic IPv6), presets, config, atomic writes,
helper argument rejection (raw IPs/paths), backend detection with mocks. Integration
(root or `unshare -rn`): `nft -c` on fixtures, apply→counter→re-apply→clear, invalid
ruleset leaves old table, unrelated dummy table survives, iptables fallback in a
container. Real host: Phase 0 script, install, reboot simulation via
`systemctl restart`, `ufw reload` survival, queue CS2 and watch counters, clear,
reboot, uninstall. Cross-distro later: Debian 12 (nft 1.0.6), Ubuntu 22.04 (1.0.2),
Fedora/firewalld, openSUSE, Alpine/Artix/Void.

**Packaging.** Arch `PKGBUILD`; generic `install.sh`/`uninstall.sh`; `.deb`/`.rpm` via
nfpm later. Dependencies: `python`, `nftables`, `python-gobject`, `gtk4`, `libadwaita`,
`polkit`; optional `ipset`. Flatpak/AppImage cannot install a root helper, polkit action
or systemd units and cannot manage the host firewall; README recommends native packages
and explains why an AppImage could only be a client for an already-installed helper.

**Open decisions:**

1. GTK4/libadwaita vs Qt6/PySide6 (GTK chosen; KDE-native trade-off above).
2. UDP-only default (recommended) vs all protocols.
3. Monitor service for live counters (recommended) vs counters only after Apply.
4. Real-host test: passed (see `docs/phase0-research.md` §3.4).
5. Paste the CLI tool you mentioned for a real review before Phase 1.
6. Phase 7 stays out of scope unless explicitly requested.
