#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi
cat <<'MSG'
cs2blocker installed.

Enable boot persistence with:
  sudo systemctl enable --now cs2blocker.service
  sudo systemctl enable --now cs2blocker-refresh.timer

Then run:
  cs2blocker-gui
MSG
exit 0
