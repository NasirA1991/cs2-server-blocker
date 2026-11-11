# CLI reference: `cs2blockctl`

`cs2blockctl` is the command-line interface to the same local relay cache,
configuration and status files used by the GUI. Read commands work as a normal
user. Commands that change firewall rules or persistent state elevate through
`pkexec` by default, or through `sudo` with `--sudo`/`sudo cs2blockctl`.

```bash
cs2blockctl --help
cs2blockctl <command> --help
man cs2blockctl
```

The command examples below assume `cs2blockctl` is installed. A development
checkout can use `./cs2blockctl` for read-only commands; privileged commands need
an installed helper (`sudo ./packaging/install.sh`) or `--sudo`.

## Global options

| Option | Meaning |
| --- | --- |
| `--sudo` | Use `sudo` for privileged operations instead of `pkexec` |
| `--version` | Print the installed version |
| `-h`, `--help` | Show help; also available per command |

Put global options before the command: `cs2blockctl --sudo apply`.

## Commands

`POP...` arguments are one or more POP codes such as `ams`, `sto` or `fra`. Use
`cs2blockctl list` to discover codes; codes are case-insensitive.

| Command | Root | Purpose |
| --- | --- | --- |
| `list [--region REGION] [--json]` | no | List POPs, relay counts and port ranges |
| `status [--json]` | no | Show rules, selection, cache and persistence status |
| `block POP...` | yes | Make the listed POPs blocked and apply immediately |
| `unblock POP...` | yes | Make the listed POPs allowed and apply immediately |
| `only POP... [--force]` | yes | Allow only these POPs; all others become blocked |
| `preset [NAME] [--list] [--save] [--delete]` | mixed | Apply or manage presets |
| `refresh` | yes | Fetch a fresh relay list and re-apply |
| `apply` | yes | Apply the saved selection |
| `pause` | yes | Remove active rules, keep the saved selection |
| `resume` | yes | Re-apply the selection saved by pause |
| `clear` | yes | Remove rules and forget the saved selection |
| `ping [POP...] [--region REGION] [--timeout SECONDS] [--json]` | no | ICMP-ping relay addresses |
| `counters [--json] [--watch] [--interval SECONDS]` | no | Show published drop counters |
| `doctor [--json] [--privileged]` | mixed | Run diagnostics; `--privileged` adds root checks |
| `persistence enable\|disable` | yes | Enable or disable boot persistence |
| `version` | no | Print the installed version |

Region names accepted by `--region` are `Europe`, `North America`,
`South America`, `Asia`, `Oceania`, `Africa` and `Middle East` (case-insensitive).

### `list`

```bash
cs2blockctl list
cs2blockctl list --region Europe
cs2blockctl list --json
```

Shows each POP's code, location, relay count and UDP port range. `--json` is
suitable for scripts. The map and sidebar use the same bundled/cached snapshot.

### `status`

```bash
cs2blockctl status
cs2blockctl status --json
```

Reports the saved selection, whether rules are active or paused, the backend,
the relay-list revision and cache age, and boot persistence. It does not require
elevation.

### `block` and `unblock`

```bash
sudo cs2blockctl block ams fra
sudo cs2blockctl unblock ams
```

Both commands change the current selection and apply immediately. They are
**mode-aware**:

- In `block` mode, `block` adds POPs to the blocked set and `unblock` removes
  them.
- In `only` mode, the saved selection is the allow-list. `block ams` removes
  `ams` from that allow-list; it does not turn the allow-list into a block-list.
  `unblock ams` adds it back.

They never flip the selection mode. To switch between the two models
deliberately, use the GUI's **Block newly added datacenters automatically**
setting or `only`.

### `only`

```bash
sudo cs2blockctl only sto sto2
sudo cs2blockctl only --force
```

Allows only the listed POPs and blocks every other known POP. An empty selection
is refused unless `--force` is supplied. `only --force` blocks all known POPs
and will normally break matchmaking; the GUI warns below three allowed POPs for
the same reason.

### `apply`

```bash
sudo cs2blockctl apply
```

Re-applies the saved selection. This is also what the systemd boot unit runs
(apart from the paused guard). A plain `apply` clears the paused state; use
`resume` when you specifically want to restore a paused selection.

### `pause` and `resume`

```bash
sudo cs2blockctl pause
sudo cs2blockctl resume
```

`pause` removes the active nftables table or iptables chain, stops the monitor
and stores the current selection as paused. It does not modify the saved
selection or presets. `resume` re-applies that selection; if application fails,
the state remains paused.

The paused flag survives a reboot, and the boot unit and scheduled refresh do
not reactivate blocking while paused. See
[Usage: pause, resume and reset](USAGE.md#pause-resume-and-reset).

### `clear`

```bash
sudo cs2blockctl clear
```

Removes all active rules and empties the saved selection. Presets are kept.
`clear` is a full reset, not a temporary pause; use `pause` when you intend to
restore the same selection later.

### `refresh`

```bash
sudo cs2blockctl refresh
```

Fetches Valve's SDR relay document, validates it, updates the cache and
re-applies the selection. On a fetch or parse failure the previous validated
cache and active rules are kept. While paused, `refresh` updates the cache but
does not re-apply rules. The CLI returns 3 on a failed refresh; the systemd
timer uses `--timer`, which records the error but returns 0 to avoid a red unit
on a transient Valve outage.

The systemd timer runs the helper's `refresh --timer` form; the CLI form is
simply `refresh`.

### `ping`

```bash
cs2blockctl ping
cs2blockctl ping --region Europe
cs2blockctl ping ams fra --timeout 2
cs2blockctl ping --json
```

Sends ICMP probes to the selected POPs, or to all POPs with relays when no POP
or region is given. This is **not** SDR latency. Some POPs do not answer ICMP
and show `n/a`; a blocked POP can still answer ICMP. `--timeout` is per probe,
in seconds; the default is 1.0.

### `counters`

```bash
cs2blockctl counters
cs2blockctl counters --watch
cs2blockctl counters --watch --interval 1
cs2blockctl counters --json
```

Reads the monitor's published per-POP drop counters. `--watch` refreshes at the
given interval (default 2 seconds). Counters reset on each apply; pausing stops
the monitor and leaves the last values in the status file. No live data is
available while the monitor is stopped.

### `preset`

```bash
cs2blockctl preset --list
sudo cs2blockctl preset eu_only
sudo cs2blockctl preset --save "My EU set"
sudo cs2blockctl preset --delete "My EU set"
```

- `preset --list` prints built-in and user presets.
- `preset NAME` applies a preset (elevated).
- `preset --save NAME` saves the current selection under a name (elevated).
- `preset --delete NAME` deletes a user preset (elevated).

Applying a regional preset expands it to explicit POP codes against the current
snapshot. See [Usage: presets](USAGE.md#presets).

### `doctor`

```bash
cs2blockctl doctor
sudo cs2blockctl doctor
cs2blockctl doctor --json
cs2blockctl doctor --privileged
```

Runs local checks for the backend, nftables/iptables tooling, polkit, systemd
units, cache freshness and common firewall conflicts. A normal run inspects what
the current user can see; `--privileged` (or `sudo`) adds kernel and backend
checks. The command returns non-zero when it finds an error, so it is suitable
for a diagnostic script.

### `persistence`

```bash
sudo cs2blockctl persistence enable
sudo cs2blockctl persistence disable
```

Enables or disables the systemd boot service. It does not enable the weekly
refresh timer; enable `cs2blocker-refresh.timer` separately. On a system without
systemd the command reports an error; use the
[non-systemd guide](NON_SYSTEMD.md) instead.

### `version`

```bash
cs2blockctl version
cs2blockctl --version
```

Prints the installed version. `cs2blockctl version` prints the bare version;
`cs2blockctl --version` also prints the program name.

## Presets

Built-in presets are seeded on first use:

| Preset | Meaning |
| --- | --- |
| `EU only` | Allow only Europe |
| `Block Asia + Oceania` | Block Asia and Oceania |
| `Nordics` | Allow only `sto` and `sto2` |

Names with spaces need quoting. User presets are stored alongside the selection
in `/etc/cs2blocker/config.json`; they survive `clear`.

## Common workflows

Inspect the current state and relay list:

```bash
cs2blockctl status
cs2blockctl list --region Europe
cs2blockctl ping --region Europe
```

Block two datacenters and verify the counters:

```bash
sudo cs2blockctl block ams fra
cs2blockctl status
cs2blockctl counters --watch
```

Temporarily step away from filtering:

```bash
sudo cs2blockctl pause
# later
sudo cs2blockctl resume
```

Save an allow-list and restore it later:

```bash
sudo cs2blockctl only sto sto2
sudo cs2blockctl preset --save "Stockholm only"
# after other experiments
sudo cs2blockctl preset "Stockholm only"
```

## Elevation and exit codes

Privileged commands use `pkexec` and the `org.cs2blocker.helper` polkit action.
If no authentication agent is available, the command prints the exact `sudo`
fallback. `--sudo` forces `sudo`; running the helper directly as root is also
supported for init systems and scripts.

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | General error, invalid configuration, or a `doctor` check failed |
| `2` | Firewall/apply failure (and argparse usage errors) |
| `3` | Relay-list refresh failure |
| `130` | Interrupted with Ctrl-C |

For example, `cs2blockctl doctor` returns `1` when a check reports an error, and
a failed `pause`, `resume`, `apply` or `clear` returns `2`. The exact helper error
is printed; the GUI shows the same text in its error dialog.

## Related documentation

- [Usage and GUI](USAGE.md)
- [Firewall and safety](FIREWALL.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Non-systemd systems](NON_SYSTEMD.md)
