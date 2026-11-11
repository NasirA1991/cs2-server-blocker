#!/usr/bin/env bash
# End-to-end helper test in an isolated network namespace (no host impact).
# Re-execs itself under `unshare -rn` so it also works as a normal user.
set -euo pipefail

if [[ -z ${CS2BLOCKER_IN_NETNS:-} ]]; then
  if [[ ${EUID} -eq 0 ]]; then
    exec unshare -n env CS2BLOCKER_IN_NETNS=1 bash "$0" "$@"
  fi
  exec unshare -rn env CS2BLOCKER_IN_NETNS=1 bash "$0" "$@"
fi

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP=$(mktemp -d /tmp/cs2blocker-netns.XXXXXX)
export CS2BLOCKER_TEST_ROOT=$TMP/root
export PYTHONPATH=$REPO
mkdir -p "$CS2BLOCKER_TEST_ROOT"
cleanup() {
  nft delete table inet unrelated_test 2>/dev/null || true
  nft delete table inet cs2blocker 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

ip link set lo up
nft -f - <<'NFT'
table inet unrelated_test {
  counter c { }
  chain output {
    type filter hook output priority 0; policy accept;
    ip daddr 203.0.113.1 counter name c drop;
  }
}
NFT

python3 -m cs2blocker.helper apply --pops ams >/dev/null
nft list table inet cs2blocker >/dev/null
nft list table inet unrelated_test >/dev/null

python3 -m cs2blocker.helper apply --pops ams >/dev/null
nft list table inet cs2blocker >/dev/null

python3 -m cs2blocker.helper apply --mode only --pops ams,fra >/dev/null
# Avoid grep -q: its early exit plus pipefail makes nft die with SIGPIPE (141).
nft list table inet cs2blocker | grep 'set pop_' >/dev/null
python3 -m cs2blocker.helper status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["active"]; assert d["state"]["mode"] == "only"'

python3 -m cs2blocker.helper clear >/dev/null
if nft list table inet cs2blocker >/dev/null 2>&1; then
  echo "our table still exists after clear" >&2
  exit 1
fi
nft list table inet unrelated_test >/dev/null
printf 'netns integration: OK\n'
