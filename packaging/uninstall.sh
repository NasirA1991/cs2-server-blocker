#!/usr/bin/env bash
# Remove cs2blocker and (by default) its config and cache.
set -Eeuo pipefail
umask 022

ROOT=/
KEEP_CONFIG=0
ASSUME_YES=0
DAEMON_RELOAD=1
FORCE=0

usage() {
  cat <<'USAGE'
Usage: sudo ./packaging/uninstall.sh [OPTIONS]

Remove the source install, its firewall objects and, unless --keep-config is
given, its configuration and cache.

Options:
  --root DIR             Remove from DIR/usr instead of /.
  --keep-config          Keep /etc/cs2blocker and /var/lib/cs2blocker.
  --yes, -y              Do not ask for confirmation.
  --no-daemon-reload     Do not run systemctl daemon-reload or reset-failed.
  --force                Bypass the system-package guard.
  -h, --help             Show this help.

If /usr/lib/cs2blocker/install-manifest.txt exists, only files listed there are
removed. Otherwise the installer's legacy hard-coded file list is used.
USAGE
}

die() {
  printf 'uninstall: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v -- "$1" >/dev/null 2>&1
}

package_is_installed() {
  if have pacman && pacman -Qq cs2blocker >/dev/null 2>&1; then
    return 0
  fi
  if have dpkg && dpkg -s cs2blocker >/dev/null 2>&1; then
    return 0
  fi
  if have rpm && rpm -q cs2blocker >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

check_package_conflict() {
  if [[ $FORCE -eq 1 ]] || ! package_is_installed; then
    return 0
  fi
  cat >&2 <<'MSG'
uninstall: cs2blocker is managed by the system package manager.
Use the package manager to remove it:
  Arch:   sudo pacman -Rns cs2blocker
  Debian: sudo apt remove cs2blocker
  Fedora: sudo dnf remove cs2blocker
Use --force only if you deliberately want the source uninstaller.
MSG
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || die "--root needs a path"
      ROOT=$2
      shift 2
      ;;
    --keep-config)
      KEEP_CONFIG=1
      shift
      ;;
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    --no-daemon-reload)
      DAEMON_RELOAD=0
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

ROOT=$(realpath -m -- "$ROOT")
if [[ $ROOT == "/" && ${EUID} -ne 0 ]]; then
  die "run as root for a system uninstall: sudo $0"
fi
if [[ $ROOT == "/" ]]; then
  check_package_conflict
fi
if [[ $ASSUME_YES -ne 1 ]]; then
  echo "This will remove cs2blocker and clear its firewall objects."
  read -r -p "Continue? [y/N] " answer
  [[ ${answer:-} == [yY] ]] || { echo "aborted"; exit 1; }
fi

DEST="$ROOT/usr"

cleanup_firewall() {
  if [[ $ROOT == "/" ]]; then
    # Stop the services first so they cannot repopulate state while the
    # firewall objects and installed files are being removed.
    if command -v systemctl >/dev/null; then
      systemctl disable --now cs2blocker.service cs2blocker-refresh.timer cs2blocker-refresh.service cs2blocker-monitor.service 2>/dev/null || true
    fi
    if [[ -x "$DEST/lib/cs2blocker/cs2blocker-helper" ]]; then
      "$DEST/lib/cs2blocker/cs2blocker-helper" clear || true
    fi
    # Last-resort cleanup if the helper/config was already broken. This still
    # targets only our table; it never flushes the ruleset.
    if have nft; then
      nft delete table inet cs2blocker 2>/dev/null || true
    fi
    # If the helper was missing, remove its fallback chain, jumps and sets
    # directly. These names are owned by this project.
    local tool
    for tool in iptables ip6tables; do
      if have "$tool"; then
        "$tool" -w 5 -D OUTPUT -j CS2BLOCKER 2>/dev/null || true
        "$tool" -w 5 -D FORWARD -j CS2BLOCKER 2>/dev/null || true
        "$tool" -w 5 -D OUTPUT -j CS2BLOCKER_NEW 2>/dev/null || true
        "$tool" -w 5 -D FORWARD -j CS2BLOCKER_NEW 2>/dev/null || true
        "$tool" -w 5 -F CS2BLOCKER 2>/dev/null || true
        "$tool" -w 5 -X CS2BLOCKER 2>/dev/null || true
        "$tool" -w 5 -F CS2BLOCKER_NEW 2>/dev/null || true
        "$tool" -w 5 -X CS2BLOCKER_NEW 2>/dev/null || true
      fi
    done
    if have ipset; then
      local name
      while IFS= read -r name; do
        case "$name" in
          cs2b_*|cs2b6_*) ipset destroy "$name" 2>/dev/null || true ;;
        esac
      done < <(ipset list -n 2>/dev/null || true)
    fi
  fi
}

remove_manifest_files() {
  local manifest="$DEST/lib/cs2blocker/install-manifest.txt"
  if [[ -f $manifest ]]; then
    local path
    while IFS= read -r path; do
      [[ $path == /usr/* ]] || continue
      case "$path" in
        *'/../'*|*'/..') continue ;;
      esac
      rm -f -- "$ROOT$path"
    done < "$manifest"
    # The manifest lists files, not directories. Remove only now-empty
    # directories inside our own code tree.
    if [[ -d "$DEST/lib/cs2blocker" ]]; then
      find "$DEST/lib/cs2blocker" -depth -type d -empty -delete
    fi
    rm -f -- "$manifest"
    rmdir --ignore-fail-on-non-empty -- \
      "$DEST/lib/cs2blocker" "$ROOT/usr/share/licenses/cs2blocker" \
      2>/dev/null || true
  else
    # Legacy removal list for installs created before install-manifest.txt.
    rm -rf -- "$DEST/lib/cs2blocker"
    rm -f -- "$DEST/bin/cs2blockctl" "$DEST/bin/cs2blocker-gui"
    rm -f -- "$DEST/lib/systemd/system/cs2blocker.service" \
      "$DEST/lib/systemd/system/cs2blocker-refresh.service" \
      "$DEST/lib/systemd/system/cs2blocker-refresh.timer" \
      "$DEST/lib/systemd/system/cs2blocker-monitor.service"
    rm -f -- "$ROOT/usr/share/polkit-1/actions/org.cs2blocker.policy"
    rm -f -- "$ROOT/usr/share/applications/org.cs2blocker.Gui.desktop"
    rm -f -- "$ROOT/usr/share/applications/cs2blocker.desktop"
    rm -f -- "$ROOT/usr/share/icons/hicolor/scalable/apps/cs2blocker.svg"
    rm -f -- "$ROOT/usr/share/man/man1/cs2blockctl.1"
    rm -f -- "$ROOT/usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml"
    rm -f -- "$ROOT/usr/share/bash-completion/completions/cs2blockctl"
    rm -f -- "$ROOT/usr/share/zsh/site-functions/_cs2blockctl"
    rm -f -- "$ROOT/usr/share/fish/vendor_completions.d/cs2blockctl.fish"
    rm -rf -- "$ROOT/usr/share/licenses/cs2blocker"
  fi
  # This file was installed by older releases but was never part of the
  # manifest because the current desktop ID replaced it.
  rm -f -- "$ROOT/usr/share/applications/cs2blocker.desktop"
}

cleanup_firewall
remove_manifest_files

if [[ $KEEP_CONFIG -eq 0 ]]; then
  rm -rf -- "$ROOT/etc/cs2blocker" "$ROOT/var/lib/cs2blocker" "$ROOT/run/cs2blocker"
fi

if [[ $ROOT == "/" ]] && have gtk-update-icon-cache; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi

if [[ $DAEMON_RELOAD -eq 1 && $ROOT == "/" ]] && have systemctl; then
  systemctl daemon-reload || true
  systemctl reset-failed cs2blocker.service cs2blocker-refresh.service cs2blocker-monitor.service 2>/dev/null || true
fi
echo "cs2blocker removed."
