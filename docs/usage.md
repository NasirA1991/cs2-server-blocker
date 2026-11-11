# Usage

This guide covers the GUI, the `cs2blockctl` CLI, presets, and the state stored
on disk. For installation, see [Installation](installation.md).

## The selection model

A datacenter is either **Allowed** or **Blocked**. The GUI keeps the applied
state separate from the pending state:

- Editing a marker or row changes the pending selection; it does not touch the
  firewall yet.
- **Apply** commits the pending selection through the privileged helper.
- **Revert** discards the pending edits.
- **Pause** removes the active firewall rules but keeps the selection. **Resume**
  re-applies that selection.

The GUI does not have a separate block-list/allow-list switch. Each POP is
shown as **Allowed** or **Blocked**, and the **Block newly added datacenters
automatically** setting chooses what happens when a later Valve snapshot adds a
POP:

- **Off (default):** new POPs start allowed. The saved selection stores the
  blocked POPs. This is the block-list mode internally.
- **On:** new POPs start blocked. The saved selection stores the allowed POPs,
  so every POP that is not explicitly allowed is blocked. This is the
  allow-list mode internally.

Changing the setting converts the visible selection to the equivalent
allowed/blocked state and creates a pending change; it does not apply anything
until you press **Apply**.

> **Before applying a very small selection:** the GUI warns when fewer than
> three datacenters remain allowed. A one-POP selection can make matchmaking
> fail or produce very long queue times.

## GUI

Start the application:

```bash
cs2blocker-gui
```

### Map

- Click a marker to toggle that POP between Allowed and Blocked.
- Click a cluster bubble to open the list of POPs in that cluster.
- Drag to pan and scroll to zoom. The `+`, `−` and reset buttons are in the
  map's top-right corner.
- Click a region chip below the map to toggle every POP in that SDR region.
- Hover a marker for a compact callout. Selecting a POP opens the inspector
  with city, country, status, ICMP latency and drops; **Details** shows relay
  addresses and UDP port ranges.

Marker appearance reflects the applied and pending state: allowed POPs are
solid, blocked POPs are hollow with a slash, mixed clusters use a neutral
bubble, and pending edits use dashed rings. A pending bar appears over the map
with the edit count, **Revert** and **Apply**.

### Datacenters panel

The right-hand panel groups POPs by continent and country.

- Use the search box for a city, country or POP code.
- Expand or collapse continents with the chevron.
- Country rows show how many POPs are allowed. Single-POP countries appear as
  one row; countries with several POPs expand to city rows with code badges.
- Use **Allow all** / **Block all** on a continent or country row for bulk edits.
  Right-click a continent header for **Block all except…**.
- Sort by name or ping, and use the latency cap to preview a bulk selection.
  **Apply cap** turns the preview into pending changes; it does not commit them.

### Inspector and status

The inspector shows the selected datacenter's city and country, status, ICMP
latency and drops since the last apply. The status pill reports whether rules
are active, the backend, relay revision and cache age. The **Settings** menu
contains manual refresh, boot persistence, the new-datacenter policy and
**Undo last apply**.

Per-POP counters update every two seconds while the monitor service is running.
ICMP latency is refreshed periodically in the background.

### Pause, resume and reset

- **Pause** removes the active firewall rules (the nftables table or iptables
  chain), stops the monitor and keeps the saved selection. The map dims and the banner offers **Resume**.
- **Resume** restores the saved selection exactly.
- **Unblock all** is the full reset: it removes the rules and forgets the saved
  selection. Presets remain in the configuration.

Pause is useful when another game or Steam feature needs SDR. When rules are
active they are host-wide: other SDR software on the same machine (for example
Dota 2, TF2, Deadlock, or a Steam feature that uses SDR) is affected too.
Per-process/cgroup scoping is not implemented.

## CLI: `cs2blockctl`

`cs2blockctl` reads the same local cache and status files as the GUI. Commands
that change the firewall or persistent state request elevation through
`pkexec` by default. Use `--sudo` to force `sudo`, or run the command as root.

```bash
cs2blockctl --help
cs2blockctl <command> --help
man cs2blockctl
```

### Commands

| Command | Root | What it does |
| --- | --- | --- |
| `list [--region REGION] [--json]` | no | List POPs, relay counts and port ranges |
| `status [--json]` | no | Show rules, selection, cache and persistence |
| `block POP...` | yes | Make the listed POPs blocked and apply |
| `unblock POP...` | yes | Make the listed POPs allowed and apply |
| `only POP... [--force]` | yes | Allow only these POPs; all others are blocked |
| `preset [NAME] [--list] [--save] [--delete]` | mixed | Apply or manage presets |
| `refresh` | yes | Fetch a new relay list and re-apply |
| `apply` | yes | Apply the saved selection |
| `pause` | yes | Remove rules, keep the selection, stop the monitor |
| `resume` | yes | Re-apply the selection saved by pause |
| `clear` | yes | Remove rules and forget the selection |
| `ping [POP...] [--region REGION] [--timeout SECONDS] [--json]` | no | ICMP-ping relay addresses |
| `counters [--json] [--watch] [--interval SECONDS]` | no | Show published drop counters |
| `doctor [--json] [--privileged]` | mixed | Run local checks; `--privileged` adds root checks |
| `persistence enable\|disable` | yes | Enable or disable boot persistence |
| `version` | no | Print the installed version |

Use `cs2blockctl list` to find POP codes. Region names are the names shown by
`list` (`Europe`, `North America`, `South America`, `Asia`, `Oceania`,
`Africa`, `Middle East`).

`block` and `unblock` are mode-aware. In allow-list mode, `block ams` removes
`ams` from the allow-list; it does not turn the allow-list into a block-list.
The command applies the resulting selection immediately.

`only` requires at least one POP unless `--force` is supplied. An empty
allow-list with `--force` blocks all known POPs and will normally break
matchmaking.

### Examples

Inspect the relay list and current state:

```bash
cs2blockctl list --region Europe
cs2blockctl status
cs2blockctl ping --region Europe
cs2blockctl counters --watch
```

Block a few datacenters:

```bash
sudo cs2blockctl block ams fra
```

Allow only Stockholm and apply:

```bash
sudo cs2blockctl only sto sto2
```

Use a preset:

```bash
cs2blockctl preset --list
sudo cs2blockctl preset eu_only
sudo cs2blockctl preset --save "My EU set"
sudo cs2blockctl preset --delete "My EU set"
```

Pause for another game, then restore:

```bash
sudo cs2blockctl pause
sudo cs2blockctl resume
```

Full reset (rules and saved selection):

```bash
sudo cs2blockctl clear
```

Machine-readable output:

```bash
cs2blockctl list --json
cs2blockctl status --json
cs2blockctl counters --json
cs2blockctl doctor --json
```

### Elevation

When run as a normal user, privileged commands use `pkexec` and the
`org.cs2blocker.helper` polkit action. If no authentication agent is available,
the command prints the exact `sudo` fallback.

```bash
cs2blockctl --sudo apply
```

Running the helper directly as root is also supported for scripts, systemd and
non-systemd init systems:

```bash
sudo /usr/lib/cs2blocker/cs2blocker-helper apply --if-not-paused
```

## Presets

Presets are stored in `/etc/cs2blocker/config.json`. Built-in presets are seeded
on first use:

| Preset | Selection |
| --- | --- |
| `EU only` | Allow-only Europe |
| `Block Asia + Oceania` | Block Asia and Oceania |
| `Nordics` | Allow only `sto` and `sto2` |

`preset --save NAME` saves the *current selection* under that name.
`preset --list` prints the presets and `preset --delete NAME` removes one.
Applying a regional preset resolves it against the current snapshot and stores
the resulting explicit POP codes in the selection, so a later relay refresh
does not silently change an allow-only selection.

## Configuration and files

The helper owns the configuration and state files. The GUI and CLI are the
supported way to change them; hand-editing is not required for normal use.

| Path | Purpose |
| --- | --- |
| `/etc/cs2blocker/config.json` | Selection and presets, root-owned and written by the helper |
| `/var/lib/cs2blocker/sdr-730.json` | Last validated Valve relay list |
| `/var/lib/cs2blocker/state.json` | Last apply/refresh result, including pause state |
| `/run/cs2blocker/status.json` | Live counters and backend status from the monitor |
| `/run/cs2blocker/lock` | Serializes helper mutations |

`config.json` is versioned. The fields most relevant to users are:

| Field | Values | Meaning |
| --- | --- | --- |
| `selection.mode` | `block`, `only` | Block selected POPs, or allow only selected POPs |
| `selection.pops` | POP codes | The explicit current selection |
| `selection.protocol` | `udp`, `all` | Protocol scope; `udp` is the safe default |
| `selection.block_forward` | `true`, `false` | Add a forward chain for routed/container traffic |
| `backend` | `auto`, `nft`, `iptables` | Firewall backend; `auto` prefers nftables |
| `notify_on_change` | `true`, `false` | Notify when a relay-list change is seen |
| `presets` | named selections | User and built-in presets |

The `protocol=all` and `block_forward=true` options are advanced settings. They
are not exposed in the GUI and are broader than the default UDP-only rule. See
[Design](design.md#3-nftables-backend-primary) for the rule model.

## Status, counters and diagnostics

- `cs2blockctl status` reports the saved selection, active rules, cache age and
  boot persistence.
- `cs2blockctl counters` reads the monitor's published snapshot. If the monitor
  is not running or the table is inactive, there are no live counters.
- `cs2blockctl ping` is an ICMP check. It is not Valve's SDR latency, and some
  relays do not answer ICMP.
- `cs2blockctl doctor` checks the backend, nftables, polkit, units and common
  firewall conflicts. `sudo cs2blockctl doctor` or `doctor --privileged` adds
  the checks that need root.

For recovery steps, see [Troubleshooting](troubleshooting.md).
