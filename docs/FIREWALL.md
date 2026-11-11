# Firewall and safety model

CS2 Server Blocker is a host firewall manager, not a per-game sandbox. This page
describes what it changes, how it isolates those changes, and what to do when a
rule needs to be removed. The exact generated nftables text and implementation
notes are in [Design](design.md#3-nftables-backend-primary).

## Host-wide effects

**The rules apply to the host network namespace, not to CS2.** They are not
scoped by process, cgroup, Steam account, or game. Any local process whose
traffic matches a blocked POP is affected, including other SDR applications such
as Dota 2, Team Fortress 2, Deadlock, and Steam features that use SDR.

There is no per-process or cgroup isolation in this project. If another game
needs SDR, use `pause`; it removes the active rules while preserving the saved
selection. `resume` restores that selection. Do not use `clear` for a temporary
pause, because `clear` also forgets the selection.

The default UDP-only mode is deliberately narrow:

- It matches SDR relay **UDP** traffic on the relay port range.
- It leaves ICMP reachable, so a blocked POP may still answer `ping`.
- It does not target Steam downloads, ordinary voice chat, or unrelated TCP/UDP
  traffic.
- It is not a VPN. It does not hide your IP address, change your route, encrypt
  traffic, or make you appear in another country.

`protocol = all` is an advanced configuration that removes the UDP port match.
It is broader, can break ICMP diagnostics, and should not be treated as the
normal mode.

## What the nftables backend owns

The primary backend creates and deletes exactly one table:

```text
inet cs2blocker
```

Inside that table it creates:

- an interval set of SDR relay UDP ports;
- one address set per POP and address family for POPs that have relays;
- one named counter per POP;
- a base `output` chain that drops matching SDR traffic;
- an optional `forward` chain when `block_forward` is enabled.

It never runs `nft flush ruleset`, does not edit `/etc/nftables.conf`, and does
not delete tables, chains, sets, or counters owned by another application. A POP
with no relay endpoints gets no set, counter, or rule.

A typical rule is equivalent to:

```nft
udp dport @sdr_ports ip daddr @pop_ams \
  counter name cnt_ams drop comment "cs2blocker: drop ams relay UDP"
```

The full ruleset is built in memory, checked with `nft -c -f -`, and then applied
with `nft -f -`. The delete/destroy of the old table and creation of the new one
are one transaction. If the check or apply fails, the previous table is left in
place; `state.json` is updated only after success.

`destroy table` is used only when the nft userspace version supports it. When it
does not, the helper uses a conditional `delete table` in the same transaction.
If the kernel rejects `destroy` despite a new userspace, the backend retries once
with delete/none and reports the original error if that also fails.

### Selection modes

`selected` means the explicit POP codes stored in the configuration.

| Mode | Which POPs are blocked |
| --- | --- |
| `block` | The selected POPs |
| `only` | Every known POP except the selected (allow-list) POPs |

In `only` mode, a newly appearing POP is blocked after the next snapshot unless
it is explicitly allowed. An IP address shared by a selected and a non-selected
POP is excluded from the blocked set to avoid collateral damage; the helper
reports such overlaps as warnings.

An empty `block` selection produces no drop rules. An empty `only` selection
blocks every known POP; the GUI requires confirmation, and the CLI requires
`--force`.

## iptables/ipset fallback

The fallback is used automatically when nftables is unavailable, or explicitly
when `backend = iptables` is configured. It is deliberately separate from the
nftables table:

- a dedicated chain `CS2BLOCKER` in the `filter` table;
- one jump from `OUTPUT` (and optionally `FORWARD`) to that chain;
- one `ipset` per POP and family, for example `cs2b_ams` and `cs2b6_ams`;
- one drop rule per POP/family matching the SDR relay UDP ports and the set;
- named counters on those rules.

Updates build a new chain and new sets, then swap the jump and remove the old
objects. This is **best-effort atomic**, not as strong as the nftables
transaction. A failure leaves the previous chain in place when possible, but
there is a short window during the swap.

The fallback does not use nftables and does not modify another application's
iptables chains. It does add a jump at the top of `OUTPUT`; that is the one shared
hook it touches. `ipset` is required. If it is missing, `doctor` reports the
package to install.

`ufw reload`, `firewalld --reload`, or another firewall manager can flush the
`filter` table and remove the jump. The nftables table is unaffected by those
reloads because it is a separate nftables table. Re-apply with
`sudo cs2blockctl apply` after a fallback reload.

## Interaction with ufw, firewalld and containers

CS2 Server Blocker does not disable or reconfigure ufw, firewalld, Docker, or
another firewall manager. The application's drop rules can still be evaluated
alongside theirs, and their rules can still drop traffic independently.

- With nftables, only `inet cs2blocker` is owned by this project. A reload of
  ufw/firewalld does not delete it, but their rules may still block the same
  traffic.
- With the iptables fallback, a firewall reload can remove the `CS2BLOCKER`
  jump. Re-apply, or switch to nftables.
- Docker and other container runtimes add their own forward/nat rules. Do not
  remove them to troubleshoot CS2; check the counters and the actual path first.

`doctor` reports detected firewalls and backend problems; it does not silently
change their configuration.

## Forwarding, VMs and containers

The default nftables chain is in the `output` hook and covers traffic originated
by the host. If CS2 runs in a VM or container, the host may see the traffic as
**forwarded**, not output.

For that case, the helper supports an optional forward chain:

```bash
sudo /usr/lib/cs2blocker/cs2blocker-helper apply --forward
```

The persistent form is `selection.block_forward = true` in the configuration.
This is an advanced setting and is not exposed in the GUI. It also requires
routing/forwarding setup for the VM or container; the application does not
configure NAT, bridges, or routes.

Without the forward chain, a host-only block may have no effect on a VM/container
that bypasses the host output path. This is a routing limitation, not evidence
that the rule is broken.

## Safe removal and recovery

Use the least destructive action that solves the problem:

| Goal | Command |
| --- | --- |
| Temporarily stop filtering, keep the selection | `sudo cs2blockctl pause` |
| Restore a paused selection | `sudo cs2blockctl resume` |
| Remove rules and forget the selection | `sudo cs2blockctl clear` |
| Remove the package and its firewall objects | package manager removal, or `sudo ./packaging/uninstall.sh --yes` |

`pause` keeps the selection and presets. `clear` keeps presets but forgets the
saved selection. The GUI's **Unblock all** performs the same reset as `clear`
after confirmation.

Package removal hooks clear the nftables table and the fallback chain before the
helper files disappear. If a broken removal left objects behind, inspect and
remove only this project's objects:

```bash
sudo nft list table inet cs2blocker
sudo iptables -w -S CS2BLOCKER
sudo ipset list -n | grep '^cs2b'
```

Then remove the jump, chain and sets if they are this project's:

```bash
sudo iptables -w -D OUTPUT -j CS2BLOCKER 2>/dev/null || true
sudo iptables -w -D FORWARD -j CS2BLOCKER 2>/dev/null || true
sudo iptables -w -F CS2BLOCKER 2>/dev/null || true
sudo iptables -w -X CS2BLOCKER 2>/dev/null || true
```

Repeat with `ip6tables` when IPv6 rules were applied. Remove the nftables table
only when you are sure it is this project's table:

```bash
sudo nft delete table inet cs2blocker
```

**Never run `nft flush ruleset` to remove this tool.** It deletes unrelated
firewall rules and can cut off networking.

## Verifying what is active

```bash
cs2blockctl status
cs2blockctl counters
sudo nft list table inet cs2blocker
sudo iptables -w -S CS2BLOCKER
```

`status` is the user-facing source of truth: it reads the saved rule state and
reports whether the service is active, paused, or has an error. `nft` and
`iptables` show the kernel objects directly. A successful ICMP ping is not proof
that a UDP rule is missing; see [latency and ICMP](USAGE.md#latency-and-ping).

For symptoms and fixes, continue with [Troubleshooting](TROUBLESHOOTING.md).
