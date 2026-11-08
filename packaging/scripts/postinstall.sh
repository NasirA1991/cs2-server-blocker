#!/bin/sh
set -e
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
