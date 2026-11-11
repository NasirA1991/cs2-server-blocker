# Phase 0 evidence

These files are historical research artifacts, not installed application code.
The current user documentation is in [`docs/`](../docs/README.md), and the
narrative report is [`docs/phase0-research.md`](../docs/phase0-research.md).

| file | purpose |
|---|---|
| `sdr-730-20261108.json` | raw CS2 SDR config, revision `1793736599`, 53 POPs / 220 relays |
| `phase0_report.py` | validates the schema, prints the region table, detects anomalies, optionally pings every relay |
| `phase0-nft-test.sh` | one-POP nftables drop test with named counter and cleanup; requires root |

## Report

```bash
python3 research/phase0_report.py            # saved snapshot, table without ping
python3 research/phase0_report.py --ping     # one ICMP echo per relay (about 4 s)
python3 research/phase0_report.py --fetch --json-out research/sdr-730-new.json
```

The report is the source of the table in `docs/phase0-research.md`. The saved snapshot
is also the input for the Phase 1 fixtures.

## Host nftables test

```bash
sudo research/phase0-nft-test.sh ams
```

It creates only `inet cs2blocker_phase0`, checks the ruleset with `nft -c`, pings the
relay before/after, sends UDP to the SDR port range, prints the counter, and removes the
table on exit. It never flushes another table and never touches `inet cs2blocker` (the
real tool's table).

The same script was exercised in an isolated network namespace during research:

```bash
unshare -rn sh -c 'ip link set lo up; TARGET_IP=127.0.0.1 bash research/phase0-nft-test.sh ams'
```

That test proves the rule/counter/cleanup mechanics without root or host impact. The
host run was exercised with `sudo` during Phase 0; rerun it only when revalidating the
raw nftables mechanics.

## Why the test uses UDP

The recommended production rule is UDP-only. A netfilter `drop` in the `output` hook makes
the local `sendto()` return `EPERM`; that is expected and is shown by the script. ICMP is
deliberately left working so the GUI ping column remains useful while a POP is blocked.
