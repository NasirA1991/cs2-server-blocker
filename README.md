# CS2 Server Blocker for Linux

[![CI](https://github.com/NasirA1991/cs2-server-blocker/actions/workflows/ci.yml/badge.svg)](https://github.com/NasirA1991/cs2-server-blocker/actions/workflows/ci.yml)
[![AUR publish](https://github.com/NasirA1991/cs2-server-blocker/actions/workflows/aur.yml/badge.svg)](https://github.com/NasirA1991/cs2-server-blocker/actions/workflows/aur.yml)
[![Latest release](https://img.shields.io/github/v/release/NasirA1991/cs2-server-blocker?display_name=tag)](https://github.com/NasirA1991/cs2-server-blocker/releases/latest)
[![AUR](https://img.shields.io/aur/version/cs2blocker)](https://aur.archlinux.org/packages/cs2blocker)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

CS2 Server Blocker is a Linux firewall manager for choosing which
Counter-Strike 2 Steam Datagram Relay (SDR) datacenters the game can reach. It
shows Valve's relay network on an interactive world map, lets you allow or block
individual POPs, and applies the selection through the host firewall (nftables
by default, with an iptables/ipset fallback). It does not modify the game,
Steam, or any other firewall object.

![CS2 Server Blocker: world map, datacenter sidebar and selected datacenter](docs/screenshot.png)

*The GTK4 interface with the world map, datacenter panel and inspector. See the
[animated demo](docs/demo.gif).*

> **Scope:** CS2 Server Blocker is a firewall rule manager. It does not read,
> modify, inject into, or locate the game, its files, its memory, or Steam. It
> never runs `nft flush ruleset`; the only nftables object it owns is
> `inet cs2blocker` (the iptables fallback owns a `CS2BLOCKER` chain).

## Features

### Pick datacenters visually

- An interactive world map with one marker per POP that has relay endpoints.
  Click a marker to toggle its state, click a cluster to inspect the POPs it
  contains, drag to pan, and scroll to zoom.
- A searchable **Datacenters** panel grouped by continent and country, with
  collapsible sections, per-country and per-continent bulk actions, and
  name/ping sorting. The inspector summarizes the focused POP; its **Details**
  section lists relay addresses and ports and reports drop counters.
- Allowed POPs, blocked POPs, mixed clusters and pending edits have distinct
  marker styles, so the map always shows the difference between the applied
  state and unsaved changes.
- A latency-cap slider previews which POPs would be affected and turns the
  result into pending changes for review.

### Control the firewall explicitly

- **Allowed / Blocked** is the primary state everywhere; a checked row means
  allowed. There is no hidden "block list vs allow list" switch in normal use.
- **Apply** is the only action that commits pending selection edits. Pending
  changes, **Revert**, and the number of unsaved edits remain visible until you
  apply or discard them.
- SDR matchmaking and relay measurement use UDP. The default rule therefore
  disables a POP without blocking ICMP, so the latency readout remains
  available while a POP is blocked.
- Boot persistence and a weekly relay-list refresh are available through
  systemd. The app can also operate without persistence or systemd.
- **Pause** removes the firewall rules while keeping the saved selection;
  **Resume** restores it. **Unblock all** is the full reset that also forgets
  the selection.

### Work from the GUI or the terminal

- GTK4/libadwaita GUI with keyboard navigation, map zoom controls, native
  dialogs, light/dark theme support, and a responsive datacenter panel.
- `cs2blockctl` provides the same selection, apply, pause, resume, refresh,
  status, ping, counter and diagnostic operations for scripts and remote use.
- Presets, Bash/Zsh/Fish completions, AppStream metadata, and a desktop
  launcher are included.

## Requirements

| Component | Requirement |
| --- | --- |
| System | Linux with nftables; iptables + ipset is a fallback when nftables is unavailable |
| Helper and CLI | Python 3.11 or newer |
| GUI | GTK4, libadwaita 1.5 or newer, PyGObject and `python-cairo` |
| Elevation | polkit with an authentication agent, or `sudo` |
| Boot persistence | systemd (optional; the GUI and CLI also work without it) |

The distro-specific package names, optional dependencies, and non-systemd setup
are in the [installation guide](docs/installation.md).

## Install

The easiest path is the package for your distribution. Download it from the
[latest release](https://github.com/NasirA1991/cs2-server-blocker/releases/latest)
and install it locally:

| Distribution | Command |
| --- | --- |
| Arch Linux, CachyOS, Manjaro | `yay -S cs2blocker` |
| Debian, Ubuntu, Linux Mint | `sudo apt install ./cs2blocker_*_amd64.deb` |
| Fedora, RHEL-compatible | `sudo dnf install ./cs2blocker-*x86_64.rpm` |
| Other distributions | extract the tarball and run `sudo ./packaging/install.sh` |

Packages install the application but do not enable or start systemd units. To
keep the selection active across reboots and refresh Valve's relay list
periodically:

```bash
sudo systemctl enable --now cs2blocker.service
sudo systemctl enable --now cs2blocker-refresh.timer
cs2blocker-gui
```

The first **Apply** asks for authentication through polkit. On a minimal
window manager without a polkit agent, run `sudo cs2blockctl apply`, or use
`cs2blockctl --sudo ...` for CLI operations.

See the [installation guide](docs/installation.md) for dependencies,
manual installation, non-systemd systems, upgrades and removal.

## First use

1. Start `cs2blocker-gui`.
2. Click a datacenter marker, a region chip, or a row in the **Datacenters**
   panel. The inspector shows the city, country, status, ICMP latency and drop
   count. Use **Details** for relay addresses and ports.
3. Press **Apply** to commit the pending selection. Pause removes the rules
   without losing the selection; Resume restores them.

A minimal CLI session:

```bash
cs2blockctl list --region Europe
cs2blockctl ping --region Europe
sudo cs2blockctl only sto sto2
sudo cs2blockctl pause
sudo cs2blockctl resume
```

`block` and `unblock` are mode-aware: they change the current selection without
silently turning an allow-list into a block-list. See the
[usage and CLI reference](docs/usage.md) for all commands, presets, counters and
diagnostics.

## How it works

- The helper fetches Valve's SDR configuration from
  `api.steampowered.com`, validates it, and caches the last good copy.
- It generates one nftables table, `inet cs2blocker`, with per-POP sets and
  counters. The complete ruleset is checked with `nft -c` and then applied in a
  single transaction. A failed check leaves the previous rules untouched.
- The unprivileged GUI and CLI read status and counter files. Only a short-lived
  root helper changes rules; the GUI never runs as root.
- The monitor service publishes live per-POP counters to `/run/cs2blocker`.
- The map is drawn locally from public-domain Natural Earth data and uses no
  online tiles or map service.

The architecture, trust boundaries, rule generation and failure handling are
described in [docs/design.md](docs/design.md).

## Limitations and safety

- **The rule is host-wide.** Blocking a POP affects every SDR user on the same
  machine, not only CS2. Use **Pause** when another game needs SDR.
- **ICMP is not SDR latency.** The displayed ping is an ICMP round-trip time, not
  Valve's SDR measurement. Some relays do not answer ICMP and show `n/a`.
  True SDR RTT would require Valve's authenticated SteamNetworkingSockets/SDR
  ticket state and is not implemented.
- **This is not a VPN or a per-process firewall.** It does not hide your IP,
  change your route, or scope rules to one game. Per-process/cgroup scoping is
  not implemented.
- **UDP-only by default.** The helper supports an all-protocol mode for advanced
  use, but it is broader, can break ICMP-based diagnostics, and is not the GUI
  default.
- **Windows and macOS are not supported.** This tool manages Linux netfilter.
- **No Flatpak or AppImage build.** A sandbox cannot install the root helper
  and polkit action needed to manage the host firewall; systemd integration is
  optional.
- **`drop` can surface as `EPERM`.** A local `sendto()` to a blocked relay can
  return `EPERM`; this is normal Linux behaviour, not a tool failure.

See [troubleshooting](docs/troubleshooting.md) for symptoms and recovery.

## Troubleshooting

Start with the built-in checks:

```bash
cs2blockctl doctor
sudo cs2blockctl doctor
systemctl status cs2blocker.service
journalctl -u cs2blocker.service -u cs2blocker-refresh.service -b
```

Common fixes for polkit/authentication failures, missing nftables, firewall
reloads, counters, `n/a` pings and non-systemd systems are in
[docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

| I want to… | Read |
| --- | --- |
| Install, upgrade or remove the tool | [Installation](docs/installation.md) |
| Learn the GUI, CLI and configuration | [Usage and CLI reference](docs/usage.md) |
| Fix a problem | [Troubleshooting](docs/troubleshooting.md) |
| Understand the architecture and security model | [Design](docs/design.md) |
| Build, test or contribute | [Development](docs/development.md) and [Contributing](CONTRIBUTING.md) |
| Read the original research | [Phase 0 research](docs/phase0-research.md) |
| Report a vulnerability | [Security policy](SECURITY.md) |

The [documentation index](docs/README.md) lists every document.

## Builds and releases

All release artifacts are built by GitHub Actions; local builds are for testing
only. Every push to `master` runs the test suite and builds the Arch package,
`.deb`, `.rpm`, source tarball and checksums. A `v*` tag publishes a GitHub
release, an SPDX SBOM and keyless cosign signatures for `SHA256SUMS`, and
updates the AUR package. See [docs/development.md](docs/development.md) for the
release process and local test commands.

## Map data

The continent outlines are derived from Natural Earth 1:110m land data, which
is in the public domain ([Natural Earth terms](https://www.naturalearthdata.com/about/terms-of-use/)).
The geometry is checked into the repository and drawn locally; the application
never requests map tiles.

## Roadmap

- [x] Interactive world map with per-datacenter state.
- [x] Latency-cap bulk selection with a map preview.
- [x] Pause/resume, presets, systemd persistence and CI-built release artifacts.
- [x] AppStream metadata, shell completions, SPDX SBOM and signed checksums.
- [ ] Latency-based automatic selection as a first-class periodic mode.
- [ ] Optional latency trend history.
- [ ] Preset import/export.
- [ ] Split the GUI window/state/services into smaller modules.
- [ ] Split the CLI and GUI into separate packages.
- [ ] True SDR RTT probing.

## License

MIT. See [LICENSE](LICENSE). Not affiliated with Valve.
