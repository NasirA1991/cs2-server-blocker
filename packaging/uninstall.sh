#!/usr/bin/env bash
# Remove cs2blocker and (by default) its config and cache.
set -euo pipefail

ROOT=/
KEEP_CONFIG=0
ASSUME_YES=0
DAEMON_RELOAD=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT=${2:?--root needs a path}; shift 2 ;;
    --keep-config) KEEP_CONFIG=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-daemon-reload) DAEMON_RELOAD=0; shift ;;
    -h|--help)
      cat <<'USAGE'
Usage: sudo ./packaging/uninstall.sh [--root DIR] [--keep-config] [--yes]
Removes the helper, units, polkit action, desktop file and icon. By default it
also removes /etc/cs2blocker and /var/lib/cs2blocker and clears the firewall table.
USAGE
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ROOT=$(realpath -m "$ROOT")
if [[ $ROOT == "/" && ${EUID} -ne 0 ]]; then
  echo "run as root for a system uninstall: sudo $0" >&2
  exit 1
fi
if [[ $ASSUME_YES -ne 1 ]]; then
  echo "This will remove cs2blocker and unblock its nftables table."
  read -r -p "Continue? [y/N] " answer
  [[ ${answer:-} == [yY] ]] || { echo "aborted"; exit 1; }
fi

DEST="$ROOT/usr"
if [[ $ROOT == "/" ]]; then
  if [[ -x "$DEST/lib/cs2blocker/cs2blocker-helper" ]]; then
    "$DEST/lib/cs2blocker/cs2blocker-helper" clear || true
  fi
  # Last-resort cleanup if the helper/config was already broken.  This still
  # targets only our table; it never flushes the ruleset.
  if command -v nft >/dev/null 2>&1; then
    nft delete table inet cs2blocker 2>/dev/null || true
  fi
  if command -v systemctl >/dev/null; then
    systemctl disable --now cs2blocker.service cs2blocker-refresh.timer 2>/dev/null || true
    systemctl stop cs2blocker-monitor.service 2>/dev/null || true
  fi
fi

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
rm -rf -- "$ROOT/usr/share/licenses/cs2blocker"

if [[ $KEEP_CONFIG -eq 0 ]]; then
  rm -rf -- "$ROOT/etc/cs2blocker" "$ROOT/var/lib/cs2blocker" "$ROOT/run/cs2blocker"
fi

if [[ $DAEMON_RELOAD -eq 1 && $ROOT == "/" ]] && command -v systemctl >/dev/null; then
  systemctl daemon-reload || true
  systemctl reset-failed cs2blocker.service cs2blocker-refresh.service cs2blocker-monitor.service 2>/dev/null || true
fi
echo "cs2blocker removed."
