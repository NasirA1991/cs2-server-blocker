# Troubleshooting

Start with the diagnostic commands before changing firewall rules by hand:

```bash
cs2blockctl doctor
sudo cs2blockctl doctor
sudo cs2blockctl status
systemctl status cs2blocker.service cs2blocker-refresh.timer
journalctl -u cs2blocker.service -u cs2blocker-refresh.service -b
```

`doctor` checks the firewall backend, nftables, polkit, systemd units, cache
freshness and common conflicts. `sudo cs2blockctl doctor` adds the checks that
need root access.

## Authentication and polkit

### “No agent” or authentication is cancelled

Privileged changes use polkit. The desktop session needs a polkit agent:

- KDE: `polkit-kde-agent`
- GNOME: `polkit-gnome`
- LXQt/LXDE: `lxpolkit`
- MATE: `polkit-mate`

Alternatively, use `sudo`:

```bash
sudo cs2blockctl apply
cs2blockctl --sudo apply
```

The GUI shows the exact `sudo` command when `pkexec` cannot authenticate.

### The password prompt appears but Apply still fails

Read the helper's error in the terminal (launch `cs2blocker-gui` from a terminal
to see it) or run:

```bash
sudo cs2blockctl apply
```

The helper validates the configuration and selection before touching nftables.
A malformed configuration is rejected rather than partially applied; a bad cache
is ignored in favour of the bundled snapshot, and should be refreshed. `doctor`
and `status` report the stored error.

## Firewall backend problems

### `nft` is missing or the kernel lacks nf_tables

Install nftables first. If nftables is genuinely unavailable, the helper can
use `iptables` + `ipset`; install both packages and retry:

```bash
sudo cs2blockctl doctor
sudo cs2blockctl apply
```

`doctor` reports which backend is selected and whether `ipset` is usable.

### Rules disappear after a firewall change

The application owns only `inet cs2blocker`. A manual command that runs
`nft flush ruleset` removes it; restore with:

```bash
sudo cs2blockctl apply
```

`ufw reload` and `firewalld --reload` do not remove the nftables table. The
iptables fallback is less isolated: a `ufw reload` can remove its jump. Re-apply
the rules, or use nftables.

### “Another firewall is managing the same traffic”

The app does not disable ufw, firewalld or Docker. Conflicting rules can still
drop or reject traffic before this table is evaluated. `doctor` reports the
detected firewalls. Keep the SDR rules in this tool and avoid duplicate manual
rules for the same relay ports.

## The blocked region still matches

1. Check whether the POP's counter is increasing:

   ```bash
   cs2blockctl counters --watch
   ```

   The live counters require `cs2blocker.service` and the monitor service to
   be active. If the monitor is not running, the command reports no counters.

2. Confirm what was actually applied:

   ```bash
   cs2blockctl status
   ```

   Pending GUI edits are not active until **Apply** is pressed.

3. Refresh after Valve changes its relay list:

   ```bash
   sudo cs2blockctl refresh
   ```

4. Remember that ICMP can still succeed while SDR UDP is blocked. A successful
   `ping` is expected and is not proof that the firewall rule is missing.

5. A VM or container running CS2 needs the optional forwarded-traffic chain
   and routing configuration. The GUI manages host output traffic only; the
   helper's advanced `apply --forward` path is documented in
   [Design](design.md#3-nftables-backend-primary).

If all counters stay at zero, check the selected POP code and the applied
selection before assuming the rule is broken.

## Virtual machines and containers

The default block is in the host's `output` hook, so it applies to traffic the
host itself sends. A game running inside a VM or container is usually forwarded
by the host; the host output rule will not match it.

1. Confirm the traffic path. If the guest has its own public/relay-facing
   network, the host may be routing it. If the container uses host networking,
   the normal output rule can match.
2. For a routed VM/container, enable the optional forward chain:
   `sudo /usr/lib/cs2blocker/cs2blocker-helper apply --forward`. The persistent
   form is `selection.block_forward = true`.
3. Make sure forwarding and the required routes/NAT are configured outside this
   project. CS2 Server Blocker does not create bridges, routes or NAT rules.
4. Verify with `cs2blockctl counters` and the host's nftables/iptables rules,
   not only with a ping from inside the guest.

See [Firewall: forwarding](FIREWALL.md#forwarding-vms-and-containers) for the
rule model. A host-only block cannot reliably filter traffic that bypasses the
host network namespace.

## Permission errors and ping

### `PermissionError` or `EPERM` from a game or test

A netfilter `drop` in the output hook makes a local `sendto()` return `EPERM`.
This is normal Linux behaviour when the destination is blocked. It is not an
application error.

### Ping shows `n/a`

The app uses an unprivileged ICMP socket first, then the system `ping` binary.
Some systems restrict both. The game's UDP traffic is unaffected. If you want
the latency column, make sure `ping` has the usual `cap_net_raw` capability or
is setuid root, depending on your distribution's policy.

ICMP is only a rough reachability/latency hint. It is not Valve's SDR latency.

## GUI problems

### The GUI does not start

Run it from a terminal to see the error:

```bash
cs2blocker-gui
```

Common causes are missing GTK4, PyGObject, `python-cairo`, or libadwaita older
than 1.5. Install the dependencies from the
[installation guide](INSTALLATION.md#package-dependencies-for-manual-installs).
The GUI does not need root; only Apply and other privileged actions do.

### The window uses the wrong theme

The application follows the desktop's light/dark preference through
libadwaita. Change the system colour scheme in the desktop settings; a theme
change while it is running should update the map and surfaces automatically.
Restart the app if it does not.

### Apply is disabled or asks me to revert

Apply is enabled only when there are pending changes. If a previous apply
failed, the status banner contains the error; **Revert** discards the pending
selection and returns to the applied state.

## Persistence, refresh and counters

### Rules do not survive a reboot

The boot service must be enabled:

```bash
sudo systemctl enable --now cs2blocker.service
```

Packages install the unit but deliberately do not enable it. If the service is
enabled but stays inactive, check:

```bash
systemctl status cs2blocker.service
journalctl -u cs2blocker.service -b
```

A paused selection intentionally skips the boot apply. Use `sudo cs2blockctl resume`
to restore it.

### The relay list is stale

```bash
sudo cs2blockctl refresh
systemctl list-timers cs2blocker-refresh.timer
```

`refresh` keeps the last good cache and rules if Valve is unavailable. A failed
fetch is reported by `status`, `doctor` and the GUI status pill.

### Counters do not update

The monitor is pulled in by `cs2blocker.service`:

```bash
systemctl status cs2blocker-monitor.service
sudo systemctl restart cs2blocker.service
```

Counters are published while rules are active. After **Pause**, the monitor is
stopped and counters remain at their last values until **Resume**.

### I paused and cannot find the old selection

Pause preserves the saved selection. Use:

```bash
sudo cs2blockctl resume
```

Do not use `clear` for a temporary pause: `clear` also forgets the selection.
Presets are not deleted by `clear`.

## Systemd is not available

The helper and CLI work without systemd. Use the init-system commands in the
[non-systemd guide](NON_SYSTEMD.md). Boot persistence,
the weekly timer and the GUI's live monitor are systemd-specific.

## Uninstall or reset

To remove only the firewall rules, keeping the tool and saved selection:

```bash
sudo cs2blockctl pause
```

To remove the rules and forget the saved selection as well:

```bash
sudo cs2blockctl clear
```

To remove the application, see [Removal](INSTALLATION.md#removal). Do not run
`nft flush ruleset`; it would remove unrelated firewall rules.

For a failed upgrade, reinstall the package or rerun the installer, then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart cs2blocker.service
```

## Reporting a problem

Include the output of:

```bash
cs2blockctl doctor
sudo cs2blockctl doctor
cs2blockctl status
systemctl status cs2blocker.service --no-pager
journalctl -u cs2blocker.service -b --no-pager
```

Remove any IP addresses or personal data you do not want to publish. Report
security issues through [SECURITY.md](../SECURITY.md), not a public issue.
