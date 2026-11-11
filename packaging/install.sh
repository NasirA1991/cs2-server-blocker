#!/usr/bin/env bash
# Install cs2blocker system-wide.  Never enables or starts anything.
set -euo pipefail

ROOT=/
DAEMON_RELOAD=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT=${2:?--root needs a path}; shift 2 ;;
    --no-daemon-reload) DAEMON_RELOAD=0; shift ;;
    -h|--help)
      cat <<'USAGE'
Usage: sudo ./packaging/install.sh [--root DIR] [--no-daemon-reload]
Installs to DIR/usr (DIR default /). Does not enable or start units.
USAGE
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ROOT=$(realpath -m "$ROOT")
if [[ $ROOT == "/" && ${EUID} -ne 0 ]]; then
  echo "run as root for a system install: sudo $0" >&2
  exit 1
fi
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=/usr
DEST="$ROOT$PREFIX"

install -d "$DEST/lib/cs2blocker" "$DEST/bin" "$DEST/lib/systemd/system" \
  "$ROOT/usr/share/polkit-1/actions" "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/icons/hicolor/scalable/apps" "$ROOT/usr/share/man/man1" \
  "$ROOT/usr/share/metainfo" \
  "$ROOT/usr/share/bash-completion/completions" \
  "$ROOT/usr/share/zsh/site-functions" \
  "$ROOT/usr/share/fish/vendor_completions.d"

# Package code and bundled data (inside the package, so Paths can find it).
# Never preserve the ownership of a developer checkout: the helper imports
# this code as root, so a user-owned file here is a local privilege escalation.
# Symlinks in a checkout could otherwise point at a user-writable payload that
# root would import later; shipped packages contain no symlinks.
if [[ -L "$SRC/cs2blocker" ]] || [[ -n "$(find "$SRC/cs2blocker" -type l -print -quit)" ]]; then
  echo "refusing to install: source package contains a symlink" >&2
  exit 1
fi
cp -a --no-preserve=ownership "$SRC/cs2blocker" "$DEST/lib/cs2blocker/"
find "$DEST/lib/cs2blocker" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$DEST/lib/cs2blocker" -type f -name '*.pyc' -delete
chmod -R u+rwX,go+rX,go-w "$DEST/lib/cs2blocker"
if [[ ${EUID} -eq 0 ]]; then
  chown -R 0:0 "$DEST/lib/cs2blocker"
fi

install -Dm755 "$SRC/packaging/launchers/cs2blocker-helper" "$DEST/lib/cs2blocker/cs2blocker-helper"
install -Dm755 "$SRC/packaging/launchers/cs2blockctl" "$DEST/bin/cs2blockctl"
install -Dm755 "$SRC/packaging/launchers/cs2blocker-gui" "$DEST/bin/cs2blocker-gui"

install -Dm644 "$SRC/packaging/systemd/cs2blocker.service" "$DEST/lib/systemd/system/cs2blocker.service"
install -Dm644 "$SRC/packaging/systemd/cs2blocker-refresh.service" "$DEST/lib/systemd/system/cs2blocker-refresh.service"
install -Dm644 "$SRC/packaging/systemd/cs2blocker-refresh.timer" "$DEST/lib/systemd/system/cs2blocker-refresh.timer"
install -Dm644 "$SRC/packaging/systemd/cs2blocker-monitor.service" "$DEST/lib/systemd/system/cs2blocker-monitor.service"
install -Dm644 "$SRC/packaging/polkit/org.cs2blocker.policy" "$ROOT/usr/share/polkit-1/actions/org.cs2blocker.policy"
# Keep desktop file ID aligned with the Wayland app_id: org.cs2blocker.Gui
install -Dm644 "$SRC/packaging/org.cs2blocker.Gui.desktop" "$ROOT/usr/share/applications/org.cs2blocker.Gui.desktop"
rm -f "$ROOT/usr/share/applications/cs2blocker.desktop"
install -Dm644 "$SRC/packaging/cs2blocker.svg" "$ROOT/usr/share/icons/hicolor/scalable/apps/cs2blocker.svg"
install -Dm644 "$SRC/packaging/org.cs2blocker.Gui.metainfo.xml" "$ROOT/usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml"
install -Dm644 "$SRC/packaging/completions/cs2blockctl.bash" "$ROOT/usr/share/bash-completion/completions/cs2blockctl"
install -Dm644 "$SRC/packaging/completions/_cs2blockctl" "$ROOT/usr/share/zsh/site-functions/_cs2blockctl"
install -Dm644 "$SRC/packaging/completions/cs2blockctl.fish" "$ROOT/usr/share/fish/vendor_completions.d/cs2blockctl.fish"
install -Dm644 "$SRC/packaging/man/cs2blockctl.1" "$ROOT/usr/share/man/man1/cs2blockctl.1"
install -Dm644 "$SRC/LICENSE" "$ROOT/usr/share/licenses/cs2blocker/LICENSE"

if [[ $ROOT == "/" ]] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi

if [[ $DAEMON_RELOAD -eq 1 && $ROOT == "/" ]] && command -v systemctl >/dev/null; then
  systemctl daemon-reload || true
fi

cat <<MSG
Installed cs2blocker under $ROOT.
Nothing was enabled or started. Next steps:
  sudo systemctl enable --now cs2blocker.service
  cs2blocker-gui
  cs2blockctl list

Boot persistence can also be toggled from the GUI.
MSG
