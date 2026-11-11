# Installation

CS2 Server Blocker runs on Linux and manages the host firewall. There is no
Flatpak or AppImage build: a sandboxed application cannot install the root
helper and polkit action needed to change the host firewall. systemd is optional
and only provides boot persistence, the refresh timer and the counter monitor.

For normal use, install a distribution package. Use the manual installer only
when no suitable package exists.

## Requirements

| Requirement | Notes |
| --- | --- |
| Linux | nftables is the primary backend; iptables + ipset is a fallback |
| Python 3.11+ | Used by the GUI, CLI and helper |
| GTK4 + libadwaita 1.5+ | GUI only; requires PyGObject and `python-cairo` |
| polkit + authentication agent | GUI and CLI elevation; `sudo` works without an agent |
| systemd | Optional; needed for boot persistence, the weekly refresh timer and the live counter monitor |

The helper, CLI, presets and manual apply/resume/clear work without a desktop
session. The GUI needs a graphical session.

## Package installation

Download the artifact for your distribution from the
[latest release](https://github.com/NasirA1991/cs2-server-blocker/releases/latest),
then install it locally.

### Arch Linux, CachyOS, Manjaro

Use the AUR package:

```bash
yay -S cs2blocker
```

Or install a downloaded package with `pacman`:

```bash
sudo pacman -U cs2blocker-*.pkg.tar.zst
```

### Debian, Ubuntu, Linux Mint

```bash
sudo apt install ./cs2blocker_*_amd64.deb
```

### Fedora, RHEL-compatible

```bash
sudo dnf install ./cs2blocker-*x86_64.rpm
```

The Debian and RPM packages declare their Python, GTK, libadwaita, nftables,
polkit and systemd dependencies. The Arch package declares the equivalent
dependencies, with `ipset` optional for the fallback backend.

### Verify release files (optional)

The release page includes `SHA256SUMS` and a keyless cosign signature. To check
the downloads are intact, put all release files in one directory and run:

```bash
sha256sum -c SHA256SUMS
```

With [cosign](https://docs.sigstore.dev/cosign/verifying/verify/) installed, the
signature can also be checked against this repository's release workflow:

```bash
cosign verify-blob \
  --certificate SHA256SUMS.pem \
  --signature SHA256SUMS.sig \
  --certificate-identity-regexp 'https://github.com/NasirA1991/cs2-server-blocker/.github/workflows/release.yml@refs/tags/v.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
```

The checksum check does not require cosign; the signature check additionally
confirms that `SHA256SUMS` was produced by this repository's release workflow.

### Other distributions and source checkouts

Download the source tarball and run the installer from its root:

```bash
tar xzf cs2blocker-*.tar.gz
cd cs2blocker-*/
sudo ./packaging/install.sh
```

A git checkout uses the same installer:

```bash
git clone https://github.com/NasirA1991/cs2-server-blocker.git
cd cs2-server-blocker
sudo ./packaging/install.sh
```

The installer copies the application, launchers, systemd units, polkit action,
desktop entry, icon, man page, AppStream metadata and shell completions under
`/usr`. It does **not** enable or start any service.

### Package dependencies for manual installs

The package manager handles these dependencies automatically. Install them
manually before using `packaging/install.sh`:

**Arch / CachyOS / Manjaro**

```bash
sudo pacman -S --needed python nftables polkit python-gobject python-cairo gtk4 libadwaita
sudo pacman -S --needed ipset   # optional iptables fallback
```

**Debian / Ubuntu**

```bash
sudo apt update
sudo apt install python3 nftables polkitd pkexec \
  python3-gi python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1
sudo apt install ipset   # optional iptables fallback
```

**Fedora**

```bash
sudo dnf install python3 nftables polkit python3-gobject python3-cairo gtk4 libadwaita
sudo dnf install ipset   # optional iptables fallback
```

**openSUSE**

Install `nftables`, `polkit`, `python3-gobject`, `python3-cairo`, the GTK4 and
libadwaita runtime packages, and optional `ipset` with `zypper`. Package names
vary between Leap and Tumbleweed releases, so use `zypper search` if a name is
not found.

After installing, these checks catch the common missing pieces:

```bash
nft --version
python3 -c 'import gi'
pkexec --version
```

The GUI also needs libadwaita 1.5 or newer. If it exits with a version error,
install the distribution's current libadwaita package.

## Enable boot persistence

The package installs the units but leaves them disabled. To keep the selection
active after a reboot and refresh Valve's relay list weekly:

```bash
sudo systemctl enable --now cs2blocker.service
sudo systemctl enable --now cs2blocker-refresh.timer
```

The monitor service is pulled in by `cs2blocker.service`; do not enable it
separately. You can enable or disable persistence later from the GUI's
**Settings** menu.

If you do not want boot persistence, skip these commands. The GUI and CLI can
still apply, pause, resume, clear and refresh rules for the current boot.

## Verify the installation

```bash
command -v cs2blocker-gui cs2blockctl
cs2blockctl doctor
sudo cs2blockctl doctor
```

Then start the GUI:

```bash
cs2blocker-gui
```

The first **Apply** asks for authentication through polkit. If no polkit agent
is available, the error includes the exact `sudo` fallback. You can also run:

```bash
sudo cs2blockctl apply
```

## Non-systemd systems

Artix (OpenRC), Void (runit), Alpine (OpenRC) and similar systems can run the
helper and CLI. The packaged builds declare systemd as a dependency because they
install unit files, so use `packaging/install.sh` from the source tarball on a
system that does not provide systemd. Boot persistence and the weekly timer
depend on systemd, so use the local init system or a scheduler instead.

The helper is installed at
`/usr/lib/cs2blocker/cs2blocker-helper`. For a non-systemd service that calls
`pause` on stop, call plain `apply` on start so the saved selection is restored
and the pause flag is cleared. Do not pair `pause` with `apply --if-not-paused`;
that combination intentionally skips the apply and is meant for systemd boot,
where a manual pause must survive.

**OpenRC example** (`/etc/init.d/cs2blocker`):

```sh
#!/sbin/openrc-run
description="CS2 Server Blocker firewall rules"
depend() {
    need net
    after nftables
}
start() {
    /usr/lib/cs2blocker/cs2blocker-helper apply
}
stop() {
    /usr/lib/cs2blocker/cs2blocker-helper pause
}
```

```bash
sudo chmod +x /etc/init.d/cs2blocker
sudo rc-update add cs2blocker default
```

**runit:** run `/usr/lib/cs2blocker/cs2blocker-helper apply` from
`/etc/rc.local` or a oneshot service, and call `pause` to stop without losing
the saved selection. As above, `apply --if-not-paused` is for systemd boot, not
this `pause`/`apply` pairing.

**Weekly refresh:** schedule
`/usr/lib/cs2blocker/cs2blocker-helper refresh --timer` with cron or another
scheduler. The GUI's persistence control and live monitor require systemd; the
CLI commands `apply`, `pause`, `resume`, `clear` and `refresh` still work.

## Upgrade

Install the newer package with the same package manager, or rerun
`sudo ./packaging/install.sh` from the new tarball. The configuration and
selection are preserved by package upgrades. The installer refuses to run if
the source package contains symlinks, and it installs the privileged code as
root-owned files.

After an upgrade, restart the service if it is enabled:

```bash
sudo systemctl daemon-reload
sudo systemctl restart cs2blocker.service
```

## Removal

Removing the package clears the `inet cs2blocker` firewall table before the
helper files disappear. The package manager's removal command is the preferred
method.

**Arch / AUR**

```bash
sudo pacman -Rns cs2blocker
```

**Debian / Ubuntu**

```bash
sudo apt remove cs2blocker
```

**Fedora / RHEL-compatible**

```bash
sudo dnf remove cs2blocker
```

If the iptables fallback was active and the removal hook could not clear it,
remove its jump and chain manually. Repeat with `ip6tables` if IPv6 rules were
applied:

```bash
sudo iptables -w -D OUTPUT -j CS2BLOCKER 2>/dev/null || true
sudo iptables -w -D FORWARD -j CS2BLOCKER 2>/dev/null || true
sudo iptables -w -F CS2BLOCKER 2>/dev/null || true
sudo iptables -w -X CS2BLOCKER 2>/dev/null || true
```

The AUR `post_remove` hook also removes `/etc/cs2blocker` and
`/var/lib/cs2blocker`. Debian and RPM packages may leave those directories so a
reinstall can keep the previous selection; remove them manually if they are no
longer wanted.

For a source installation, run the uninstaller from the checkout or extracted
tarball:

```bash
sudo ./packaging/uninstall.sh --yes
```

Use `--keep-config` to preserve the saved selection, presets and cache.

To remove only the firewall rules while keeping the tool and the saved
selection, pause:

```bash
sudo cs2blockctl pause
```

To remove the rules **and** forget the saved selection:

```bash
sudo cs2blockctl clear
```

Manual cleanup is only a last resort if a package was already broken. It
targets this project's paths and firewall objects only:

```bash
sudo systemctl disable --now cs2blocker.service cs2blocker-refresh.timer
sudo systemctl stop cs2blocker-monitor.service
sudo nft delete table inet cs2blocker 2>/dev/null || true
sudo iptables -w -D OUTPUT -j CS2BLOCKER 2>/dev/null || true
sudo iptables -w -D FORWARD -j CS2BLOCKER 2>/dev/null || true
sudo iptables -w -F CS2BLOCKER 2>/dev/null || true
sudo iptables -w -X CS2BLOCKER 2>/dev/null || true
sudo rm -rf /usr/lib/cs2blocker /etc/cs2blocker /var/lib/cs2blocker /run/cs2blocker
sudo rm -f /usr/bin/cs2blockctl /usr/bin/cs2blocker-gui
sudo rm -f /usr/lib/systemd/system/cs2blocker.service \
  /usr/lib/systemd/system/cs2blocker-refresh.service \
  /usr/lib/systemd/system/cs2blocker-refresh.timer \
  /usr/lib/systemd/system/cs2blocker-monitor.service
sudo rm -f /usr/share/polkit-1/actions/org.cs2blocker.policy \
  /usr/share/applications/org.cs2blocker.Gui.desktop \
  /usr/share/applications/cs2blocker.desktop \
  /usr/share/icons/hicolor/scalable/apps/cs2blocker.svg \
  /usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml \
  /usr/share/man/man1/cs2blockctl.1 \
  /usr/share/bash-completion/completions/cs2blockctl \
  /usr/share/zsh/site-functions/_cs2blockctl \
  /usr/share/fish/vendor_completions.d/cs2blockctl.fish \
  /usr/share/licenses/cs2blocker/LICENSE
sudo systemctl daemon-reload
```

If IPv6 rules were applied, repeat the four `iptables` commands with
`ip6tables`.

Never run `nft flush ruleset` to remove this tool; that deletes unrelated
firewall rules. The uninstaller and package removal hooks delete only
`inet cs2blocker`.

## Next steps

- [First use and CLI](usage.md)
- [Troubleshooting](troubleshooting.md)
- [Security model](../SECURITY.md)
