#!/usr/bin/env bash
# Phase 0 manual test: block one real CS2 POP with a temporary nftables table.
#
#   sudo research/phase0-nft-test.sh [POP]
#
# It only creates/deletes table `inet cs2blocker_phase0` and never flushes or
# edits any other table.  The trap removes the table even on failure.
#
# Expected result:
#   * ICMP still works after the rule (the rule is UDP-only).
#   * a UDP send to the relay port starts failing immediately with EPERM,
#     because netfilter drops locally generated packets in the output hook.
#   * the named counter `cnt_test` increments once per blocked datagram.
#
# Tested in an isolated network namespace with TARGET_IP=127.0.0.1.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run with sudo: sudo $0 [POP]" >&2
  exit 1
fi

POP=${1:-ams}
[[ $POP =~ ^[a-z0-9]+$ ]] || { echo "bad POP code: $POP" >&2; exit 1; }
SNAPSHOT=${SNAPSHOT:-$(dirname "$0")/sdr-730-20261108.json}
TABLE=cs2blocker_phase0
PORT_LO=27015
PORT_HI=27140

command -v nft >/dev/null || { echo "nft not found" >&2; exit 1; }
[[ -r $SNAPSHOT ]] || { echo "snapshot not readable: $SNAPSHOT" >&2; exit 1; }

if [[ -n ${TARGET_IP:-} ]]; then
  TARGET_IPS=("$TARGET_IP")
else
  if ! IPS_TEXT=$(python3 - "$SNAPSHOT" "$POP" <<'PY'
import ipaddress, json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
pop = doc["pops"].get(sys.argv[2])
if pop is None:
    sys.exit(f"unknown POP: {sys.argv[2]}")
found = False
for relay in pop.get("relays", []):
    if "ipv4" in relay:
        addr = ipaddress.IPv4Address(relay["ipv4"])
        if not addr.is_global:
            sys.exit(f"refusing non-global address {addr}")
        print(addr)
        found = True
if not found:
    sys.exit(f"POP {sys.argv[2]} has no IPv4 relays")
PY
  ); then
    exit 1
  fi
  [[ -n $IPS_TEXT ]] || { echo "POP $POP has no IPv4 relays" >&2; exit 1; }
  mapfile -t TARGET_IPS <<< "$IPS_TEXT"
fi
[[ ${#TARGET_IPS[@]} -gt 0 ]] || { echo "POP $POP has no IPv4 relays" >&2; exit 1; }

ELEMENTS=""
for ip in "${TARGET_IPS[@]}"; do
  [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad target IP: $ip" >&2; exit 1; }
  ELEMENTS+="${ELEMENTS:+, }$ip comment \"$POP relay, phase0 test\""
done
TARGET_IP=${TARGET_IPS[0]}

RULES=$(mktemp /tmp/cs2blocker-phase0.XXXXXX.nft)
cleanup() {
  nft delete table inet "$TABLE" 2>/dev/null || true
  rm -f "$RULES"
}
trap cleanup EXIT INT TERM

cat >"$RULES" <<EOF
table inet $TABLE {
  counter cnt_test {
    comment "phase0 test drops for $POP"
  }
  set pop_test {
    type ipv4_addr
    flags interval
    elements = { $ELEMENTS }
  }
  chain output {
    type filter hook output priority filter; policy accept;
    udp dport $PORT_LO-$PORT_HI ip daddr @pop_test counter name cnt_test drop comment "phase0 test: drop $POP UDP"
  }
}
EOF

udp_probe() {
  python3 - "$TARGET_IP" "$PORT_LO" <<'PY'
import socket, sys
ip, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _ in range(3):
    try:
        s.sendto(b"cs2blocker phase0 probe", (ip, port))
    except OSError as exc:
        print(f"UDP send -> {ip}:{port}: BLOCKED ({exc.__class__.__name__}: {exc})")
        break
else:
    print(f"UDP send -> {ip}:{port}: allowed (3 datagrams queued)")
PY
}

echo "== target =="
echo "POP=$POP  UDP ports $PORT_LO-$PORT_HI  (${#TARGET_IPS[@]} relay IPv4 addresses)"
printf '  relay: %s\n' "${TARGET_IPS[@]}"
echo
echo "== before: ICMP and UDP =="
ping -c 2 -W 2 "$TARGET_IP" || true
udp_probe
echo
echo "== validate ruleset with nft --check (no changes) =="
nft -c -f "$RULES"
echo "check OK"
echo
echo "== apply temporary table =="
nft -f "$RULES"
nft list table inet "$TABLE"
echo
echo "== after: ICMP should still work, UDP should be dropped =="
ping -c 2 -W 2 "$TARGET_IP" || true
udp_probe
sleep 0.2
echo
echo "== counters =="
nft list table inet "$TABLE" | sed -n '/counter cnt_test/,/}/p'
echo
echo "== remove temporary table =="
nft delete table inet "$TABLE"
trap - EXIT INT TERM
rm -f "$RULES"
udp_probe
echo "table removed; UDP should work again"
