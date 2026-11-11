# Documentation

CS2 Server Blocker is a Linux firewall manager for selecting CS2 SDR
datacenters. The README is the project overview; this page points to the
detailed guides.

## For users

| Document | Contents |
| --- | --- |
| [Installation](installation.md) | Package installation, dependencies, manual installs, non-systemd setups, upgrades and removal |
| [Usage](usage.md) | GUI walkthrough, CLI reference, presets, configuration and status files |
| [Troubleshooting](troubleshooting.md) | Authentication, firewall, counters, GUI and persistence problems |
| [Security policy](../SECURITY.md) | How to report vulnerabilities and the project's threat model |

## For contributors

| Document | Contents |
| --- | --- |
| [Development](development.md) | Test commands, repository layout, release process and documentation rules |
| [Contributing](../CONTRIBUTING.md) | Reporting bugs, submitting changes and review expectations |
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
