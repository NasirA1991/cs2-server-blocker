# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could let a local
user gain privileges, bypass the firewall, or corrupt root-owned state. Use
GitHub's **Report a vulnerability** flow on the repository's Security tab, or
email the maintainer at `nasir@users.noreply.github.com`. Include the version,
distribution, and a minimal reproduction. You can expect an initial reply
within a few days.

## Threat model

CS2 Server Blocker has two pieces:

- The unprivileged GUI/CLI, which parses Valve's relay data and asks for an
  action.
- A small root helper, started through polkit or `sudo`, which validates the
  request and writes nftables rules.

The helper treats Valve's API response and all command-line input as
untrusted. It validates POP codes, relay addresses, ports, modes, and file
sizes before rendering rules. It owns exactly one nftables table,
`inet cs2blocker`, and never runs `nft flush ruleset`.

The helper's Python files must be root-owned and not group/world-writable.
Packages install them that way; `packaging/install.sh` explicitly drops the
invoking user's ownership and refuses to preserve it. The helper and `doctor`
check this at startup. A source checkout is not trusted for system use.

The helper does **not** protect against an attacker who already has root.
ICMP is deliberately not blocked, so a blocked POP can still answer ping. The
firewall is host-wide: other SDR users on the same machine are affected too.

## Supported versions

Security fixes are applied to the latest release. Version 0.2.0 adds AppStream
metadata, shell completions, signed release checksums with an SPDX SBOM,
theme-aware dialogs, and broader firewall integration tests. Version 0.1.4 added
pause/resume, installed-code symlink checks, and the boot-time paused guard.
Version 0.1.3 was the first release with the root-owned-code, package-removal,
and mode-aware CLI fixes.
