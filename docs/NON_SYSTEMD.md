# Non-systemd systems

CS2 Server Blocker does not require systemd to filter traffic. The helper, CLI,
presets and manual `apply`/`pause`/`resume`/`clear` operations work on any Linux
system with a supported firewall backend.

systemd is used for three conveniences only:

- boot persistence;
- the weekly relay-list refresh timer;
- the live counter monitor.

The GUI and CLI still work without those services; you provide equivalent
startup and scheduling with your init system or a scheduler.

## Install without systemd

The distribution packages install systemd unit files and may declare systemd as
a dependency. On a non-systemd system, use the source tarball and the generic
installer instead:

```bash
tar xzf cs2blocker-*.tar.gz
cd cs2blocker-*/
sudo ./packaging/install.sh
```

The installer copies the same helper, CLI, GUI, polkit action, desktop entry,
man page and completions under `/usr`. It only runs `systemctl daemon-reload`
when `systemctl` exists; on a non-systemd system that step is skipped.

For a distribution package, check whether your packaging system can install with
`--nodeps` or an equivalent. Do not remove a systemd dependency blindly: verify
that the package's post-install scripts do not require it.

## The helper is the service entry point

The installed helper is:

```text
/usr/lib/cs2blocker/cs2blocker-helper
```

It must run as root. The normal operations are:

```bash
sudo /usr/lib/cs2blocker/cs2blocker-helper apply
sudo /usr/lib/cs2blocker/cs2blocker-helper pause
sudo /usr/lib/cs2blocker/cs2blocker-helper resume
sudo /usr/lib/cs2blocker/cs2blocker-helper clear
sudo /usr/lib/cs2blocker/cs2blocker-helper refresh
```

`apply` restores the saved selection and clears the paused flag. `pause` removes
the rules but keeps the selection. `resume` restores a paused selection. `clear`
removes the rules and forgets the selection.

Do not run the helper as an unprivileged user; it refuses to run without root.

## OpenRC

The following is a starting point, not a shipped init script. Test it on your
system before enabling it.

`/etc/init.d/cs2blocker`:

```sh
#!/sbin/openrc-run

description="CS2 Server Blocker firewall rules"
depend() {
    need net
    after nftables
}

start() {
    ebegin "Applying CS2 Server Blocker rules"
    /usr/lib/cs2blocker/cs2blocker-helper apply
    eend $?
}

stop() {
    ebegin "Pausing CS2 Server Blocker rules"
    /usr/lib/cs2blocker/cs2blocker-helper pause
    eend $?
}
```

```bash
sudo chmod 755 /etc/init.d/cs2blocker
sudo rc-update add cs2blocker default
sudo rc-service cs2blocker start
```

OpenRC systems include Alpine, Artix (OpenRC profile), Gentoo and some others.
Make sure the nftables service (or the iptables/ipset packages for the fallback)
is installed and starts before this service. The exact service name differs by
distribution; `rc-service --list` shows what is available.

On Alpine, install the runtime with `apk`; package names can change between
releases, so search if a name is rejected:

```bash
sudo apk add nftables python3 py3-gobject3 py3-cairo gtk4.0 libadwaita
sudo apk add ipset   # optional iptables fallback
```

On Artix/OpenRC, use `pacman` and enable the nftables service with `rc-update`
if your installation provides one.

## runit

Void Linux and Artix/runit use runit. runit supervises **long-running** services;
the helper's `apply` command is a oneshot. Do not put a bare `exec apply` in a
runit `run` script unless you want runit to restart it repeatedly.

Use the init system's boot hook for the oneshot apply, and a runit service only
for the long-running monitor. On Void, `/etc/rc.local` runs at boot; add:

```sh
/usr/lib/cs2blocker/cs2blocker-helper apply
```

If your system provides a shutdown hook (`/etc/rc.shutdown` on some runit
setups), add the matching pause:

```sh
/usr/lib/cs2blocker/cs2blocker-helper pause
```

For live counters, create a runit service for the monitor:

```bash
sudo mkdir -p /etc/sv/cs2blocker-monitor
```

`/etc/sv/cs2blocker-monitor/run`:

```sh
#!/bin/sh
exec /usr/lib/cs2blocker/cs2blocker-helper monitor --interval 2
```

```bash
sudo chmod 755 /etc/sv/cs2blocker-monitor/run
sudo ln -s /etc/sv/cs2blocker-monitor /var/service/cs2blocker-monitor
```

The helper creates `/run/cs2blocker` and the state/config directories on first
use. The monitor stays in the foreground and publishes counters while the rules
are active.

On Void, install the runtime with `xbps-install`:

```bash
sudo xbps-install -S nftables python3 python3-gobject python3-cairo gtk4 libadwaita
sudo xbps-install -S ipset   # optional iptables fallback
```

If your runit variant has a supported oneshot mechanism, use that instead of
the boot hook; the helper must still run as root.

## Pause across reboots

The systemd unit starts with:

```text
cs2blocker-helper apply --if-not-paused
```

That guard means a manual pause survives a reboot. A plain `apply` at boot will
instead restore the selection and clear the paused flag.

Choose deliberately:

- **Restore filtering at boot:** use plain `apply` in your init script. A manual
  pause lasts until the next boot.
- **Keep pause across boots:** use `apply --if-not-paused` in the boot path. To
  restore filtering after a pause, run `resume` manually.

Do not pair `pause` on stop with `apply --if-not-paused` on start and expect a
normal restart to resume filtering; that combination intentionally skips the
apply while the paused flag is set.

## Scheduled refresh

There is no systemd timer on a non-systemd system. Schedule the helper with cron,
anacron, or your init system's timer equivalent. A weekly cron entry:

```cron
17 4 * * 1 root /usr/lib/cs2blocker/cs2blocker-helper refresh --timer
```

The `--timer` flag is the same mode used by the systemd service. The refresh
keeps the previous validated cache and active rules if fetching or parsing
fails. While paused, refresh may update the cache but does not re-apply rules.

Do not schedule `apply --if-not-paused` as a refresh substitute; it does not
fetch a new relay list.

## Live counters and the monitor

`cs2blocker-monitor.service` is the only component that publishes live packet
drop counters. Without systemd, there is no monitor process, so the GUI and
`cs2blockctl counters` have no live values to read. Existing counters in the
kernel rules remain visible with `nft` or `iptables`.

If you want live counters, run the helper monitor under your init system:

```bash
sudo /usr/lib/cs2blocker/cs2blocker-helper monitor --interval 2
```

This is a long-running foreground process. A runit service, an OpenRC
`supervise-daemon` service, or a small wrapper can keep it alive. It expects the
runtime and state directories to exist:

```text
/etc/cs2blocker
/var/lib/cs2blocker
/run/cs2blocker
```

The root helper normally creates these on first use. Do not point the monitor at
a directory writable by an unprivileged user.

## GUI and polkit

The GUI still uses the installed polkit action to elevate Apply, Pause/Resume,
refresh, presets and persistence. On a desktop without a polkit authentication
agent, the GUI reports the failure and suggests `sudo cs2blockctl`. You can run
the CLI directly with `sudo`:

```bash
sudo cs2blockctl apply
sudo cs2blockctl pause
sudo cs2blockctl resume
```

The GUI's **Settings** menu uses `systemctl` for boot persistence. On a
non-systemd system that control reports an error; use your init system instead.
The rest of the GUI (map, sidebar, selection, apply, pause/resume) works.

## Firewall startup order

Start the firewall backend before applying rules:

- nftables: make sure the `nftables` service (if present) has loaded its own
  configuration. The application creates a separate `inet cs2blocker` table and
  does not edit `/etc/nftables.conf`.
- iptables/ipset: make sure the `filter` table and `ipset` are available. The
  fallback adds a jump to `OUTPUT`; a later firewall reload can remove it.

If your init system runs the helper before the backend is ready, move the
service after the backend or add a short retry in the init script. Do not add
`nft flush ruleset` to a startup script; it deletes unrelated firewall rules.

## Removal

Use the installer's uninstaller, which clears this project's firewall objects:

```bash
sudo ./packaging/uninstall.sh --yes
```

Add `--keep-config` to keep `/etc/cs2blocker` and `/var/lib/cs2blocker`. If the
source tree is gone, remove the package with your package manager and inspect the
firewall manually as described in
[Firewall: safe removal](FIREWALL.md#safe-removal-and-recovery).

If you created an OpenRC/runit service, remove its symlink and script too:

```bash
sudo rc-update del cs2blocker default
sudo rm -f /etc/init.d/cs2blocker
```

or, for runit:

```bash
sudo rm -f /var/service/cs2blocker
sudo rm -rf /etc/sv/cs2blocker
```

Reload the init system after removing service definitions.

## Limitations

- The GUI's boot-persistence switch and weekly timer switch are systemd-only.
- The live counter monitor is systemd-only unless you supervise it yourself.
- The packaged installers are tested on systemd distributions; non-systemd init
  scripts above are examples, not shipped, tested artifacts.
- polkit is still required for GUI elevation; `sudo` is the fallback.

For the systemd installation, see [Installation](INSTALLATION.md). For command
details, see the [CLI reference](CLI.md).
