# Documentation

CS2 Server Blocker is a Linux firewall manager for selecting CS2 SDR
datacenters. The [README](../README.md) is the project overview; this page points
to the detailed guides.

## For users

| Document | Contents |
| --- | --- |
| [Installation](INSTALLATION.md) | Dependencies, packages, source installs, upgrades and removal |
| [Usage](USAGE.md) | GUI walkthrough, map interactions, selection, presets and pause/resume |
| [CLI reference](CLI.md) | Every `cs2blockctl` command, option, privilege rule and exit code |
| [Firewall guide](FIREWALL.md) | nftables/iptables rules, isolation, host-wide effects and safe removal |
| [Troubleshooting](TROUBLESHOOTING.md) | Authentication, firewall, counters, GUI and persistence problems |
| [Non-systemd systems](NON_SYSTEMD.md) | OpenRC, runit, Alpine, Artix, Void and scheduled refresh |
| [Security policy](../SECURITY.md) | Vulnerability reporting and the project's threat model |

## For contributors

| Document | Contents |
| --- | --- |
| [Development](DEVELOPMENT.md) | Tests, repository layout, release process and documentation rules |
| [Contributing](../CONTRIBUTING.md) | Bug reports, pull requests and review expectations |
| [Design](design.md) | Architecture, privilege boundaries, nftables/iptables rules and failure handling |
| [Phase 0 research](phase0-research.md) | Historical evidence and measurements from the initial prototype |

## Project

- [README](../README.md)
- [License](../LICENSE)
- [Latest release](https://github.com/NasirA1991/cs2-server-blocker/releases/latest)
- [Issue tracker](https://github.com/NasirA1991/cs2-server-blocker/issues)

The source of truth for behaviour is the implementation and the tests. If a
document and the code disagree, report the discrepancy rather than assuming the
document is correct.
