#!/bin/sh
# Best-effort teardown before the package files disappear.  A package manager
# cannot leave a live inet cs2blocker table behind with no helper to remove it.
# Debian passes "remove" or "upgrade"; RPM passes 0 (remove) or 1 (upgrade).
set -e

case "${1:-}" in
    upgrade|1)
        exit 0
        ;;
esac

HELPER=/usr/lib/cs2blocker/cs2blocker-helper
if [ -x "$HELPER" ]; then
    "$HELPER" clear >/dev/null 2>&1 || true
fi

# The helper may already be broken or half-removed; target only our table and
# never flush the global ruleset.
if command -v nft >/dev/null 2>&1; then
    nft delete table inet cs2blocker >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now cs2blocker.service cs2blocker-refresh.timer >/dev/null 2>&1 || true
    systemctl stop cs2blocker-monitor.service >/dev/null 2>&1 || true
fi

exit 0
