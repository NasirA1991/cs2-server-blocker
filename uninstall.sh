#!/usr/bin/env bash
set -Eeuo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/packaging/uninstall.sh" "$@"
