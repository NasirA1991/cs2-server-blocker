# Phase 0 — Research and feasibility

**Status:** Phase 0 complete — real-host test passed. The project was subsequently
implemented end-to-end; this document is kept as the historical research evidence. For
current installation and usage, start with the [documentation index](README.md).
**Date:** 2026-11-08
**Host used for research:** CachyOS (Arch-based), KDE Plasma on Wayland, kernel 7.3.1,
nftables 1.1.7, Python 3.14.9, ufw 0.36.2 active, `nftables.service` inactive.

**Verdict:** the project is feasible with the design in [design.md](design.md). Every
mechanism that matters was exercised on this machine: the live Valve endpoint, all 220
relay IPs, unprivileged ICMP, named nftables counters, atomic ruleset replacement, and
the drop/cleanup cycle. The privileged host test was run on the real host and passed: ICMP kept working, UDP was dropped with `EPERM`, the named counter incremented,
and cleanup restored UDP. That closes the last open feasibility question. The netns run
and the real host output are both recorded in §3.

---

## 1. The SDR endpoint and JSON schema (verified from actual output)

### 1.1 Endpoint

```http
GET https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/?appid=730
```

Both `.../v1?appid=730` and `.../v1/?appid=730` return the same document. I use the
version with the explicit `/v1/` path.

Observed response (2026-11-08):

| item | value |
|---|---|
| HTTP status | `200` |
| `content-type` | `application/json; charset=UTF-8` |
| `content-length` | `28807` |
| `cache-control` | `public, max-age=46` |
| ETag / Last-Modified | none |
| `success` | `true` |
| `revision` | `1793736599` |
| snapshot SHA-256 | `254aed2fe4607ed315de273301b3f63578969b041629b21f5cfb68d91fc1d8e1` |

A bad appid produces **HTTP 500** with a JSON body, not an HTTP 200 with `success:false`:

```console
$ curl -sS 'https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/?appid=999999999'
{"success":false,"message":"Failed to get appinfo"}
```

So the parser must check both the HTTP status and `success is True`; it may not treat
"we got JSON" as success. Network errors, proxy errors, captive portals and HTML error
pages must all be treated as a failed refresh.

### 1.2 Top-level shape

From the real document, top-level keys are:

```text
certs, p2p_share_ip, pops, relay_public_key, revision, revoked_keys, success, typical_pings
```

Only `success`, `revision` and `pops` are needed for blocking. `certs`,
`relay_public_key` and `revoked_keys` are for the SDR protocol itself; we do not need
them. `typical_pings` is a matrix of median POP-to-POP latencies (196 entries in this
snapshot) and could be useful later for a "you blocked the POP closest to you" warning,
but it is not a substitute for a real ping from the user's machine.

### 1.3 POP and relay shape

Real excerpt (pretty-printed, one relay shown):

```json
{
  "revision": 1793736599,
  "success": true,
  "pops": {
    "ams": {
      "desc": "Amsterdam (Netherlands)",
      "geo": [4.9, 52.37],
      "partners": 1,
      "tier": 1,
      "relays": [
        {"ipv4": "155.133.248.36", "port_range": [27015, 27060]},
        {"ipv4": "155.133.248.37", "port_range": [27015, 27060]}
      ]
    }
  }
}
```

Facts that matter for the design:

| fact | observed | consequence |
|---|---|---|
| POPs | 53 | small enough to list all |
| relay endpoints | 220 | one named set / counter per POP is cheap |
| IPv6 relays | **0** | build IPv6 support now, but it will not appear on screen yet |
| relay keys | only `ipv4` + `port_range` | parse `ipv6` defensively for the future |
| `port_range` | `[start, end]`, inclusive | 27015–27044, 27015–27060, 27015–27078 or 27015–27140 |
| `geo` order | `[longitude, latitude]` | do not assume `[lat, lon]` |
| region field | **none** | region must be curated by POP code; see §2 |
| aliases | `bom2→bom`, `eat→mwh`, `hel→helm`, `maa2→maa`, `tyo→tyo1,tyo2,tyo3` | alias codes are not keys in `pops`; resolve or ignore them |
| POPs without `relays` | **8**, and the key is absent rather than `[]` | always use `.get("relays", [])`; show "no relays" in UI |
| IPs shared between POPs | **0** today | still implement overlap handling for future data |
| IPs duplicated inside one POP | 0 | dedupe while parsing |
| private/loopback/link-local relays | 0 | reject anyway; the data is untrusted input |
| POP ordering | alphabetical by code | carry our own deterministic ordering |

The eight POPs with no relay entries in this snapshot are:

```text
ctu (Alibaba Cloud Chengdu), dsto (Datapacket Stockholm), eat (Wenatchee),
fsn (Hetzner Falkenstein), hel (Hetzner Helsinki), pek (Alibaba Cloud Beijing),
pvg (Alibaba Cloud Shanghai), tgd (Tencent Guangzhou)
```

The Chinese parent POPs have separate `*m`/`*t`/`*u` mobile/telecom/unicom children with
actual relays. The others look like partner placeholders or POPs whose relays are not
announced to this appid. They can still appear in the game's POP list, but blocking them
has no effect because there are no IPs to block. The UI should show `0 relays` and the
rule generator must skip them.

### 1.4 One real data anomaly

`dsto` has `"desc": "Datapacket Stockholm (Sweden)"` but `"geo": [37.62, 55.75]`, which is
Moscow, Russia. This is Valve's data, not a parsing bug. It is another reason **not** to
derive the region from coordinates: a geo-based mapper would classify Stockholm as
Europe/Asia depending on the bounding box, and the desc is the only trustworthy label.
The curated code→region map is authoritative; `geo` is display-only.

### 1.5 Other Valve games (Phase 7 feasibility, not Phase 1 scope)

The same endpoint and schema work for other appids:

| appid | game | POPs | relays | IPv6 |
|---|---|---:|---:|---:|
| 730 | CS2 | 53 | 220 | 0 |
| 570 | Dota 2 | 70 | 175 | 0 |
| 1422450 | Deadlock | 37 | 151 | 0 |
| 440 | Team Fortress 2 | 37 | 151 | 0 |

The config is not identical per game (Dota has more POPs), so per-appid caches are
required. Phase 7 is therefore plausible, but it is explicitly optional and needs your
approval.

---

## 2. POPs grouped by region

**Important:** Valve does not provide a region field. This table uses a curated
`POP code → region` map (the same map will live in `cs2blocker/core/regions.py`).
Unknown future codes fall into `Unknown` and are shown separately rather than guessed.

The RTT column is **one unprivileged ICMP echo per relay**, sent from this machine in
Norway on 2026-11-08. It is indicative only: the game uses SDR's own UDP measurements,
not ICMP, and ICMP can be filtered on some networks. `—` means no relay to ping.

| POP | Location | Relays | Port range(s) | ICMP replies | min RTT |
|---|---|---:|---|---|---:|
| **Europe** (15 POPs) | | | | | |
| `ams` | Amsterdam (Netherlands) | 4 | 27015–27060 | 4/4 | 26.1 ms |
| `dfra` | Datapacket Frankfurt (Germany) | 2 | 27015–27060 | 2/2 | 27.7 ms |
| `dlhr` | Datapacket London (England) | 2 | 27015–27060 | 2/2 | 34.1 ms |
| `dsto` | Datapacket Stockholm (Sweden) | 0 | — (no relays) | 0/0 | — |
| `dvie` | Datapacket Vienna (Austria) | 2 | 27015–27060 | 2/2 | 37.0 ms |
| `fra` | Frankfurt (Germany) | 16 | 27015–27140 | 16/16 | 27.3 ms |
| `fsn` | Hetzner Falkenstein (Germany) | 0 | — (no relays) | 0/0 | — |
| `hel` | Hetzner Helsinki (Finland) | 0 | — (no relays) | 0/0 | — |
| `lhr` | London (England) | 4 | 27015–27060 | 4/4 | 31.4 ms |
| `mad` | Madrid (Spain) | 4 | 27015–27060 | 4/4 | 59.8 ms |
| `par` | Paris (France) | 4 | 27015–27060 | 4/4 | 35.6 ms |
| `sto` | Stockholm - Kista (Sweden) | 11 | 27015–27060, 27015–27140 | 11/11 | 15.4 ms |
| `sto2` | Stockholm - Bromma (Sweden) | 10 | 27015–27060 | 10/10 | 14.0 ms |
| `vie` | Vienna (Austria) | 6 | 27015–27060 | 6/6 | 39.6 ms |
| `waw` | Warsaw (Poland) | 6 | 27015–27140 | 6/6 | 31.3 ms |
| **North America** (7 POPs) | | | | | |
| `atl` | Atlanta (Georgia) | 4 | 27015–27140 | 4/4 | 108.7 ms |
| `dfw` | Dallas (Texas) | 4 | 27015–27140 | 4/4 | 140.3 ms |
| `eat` | Wenatchee (Washington) | 0 | — (no relays) | 0/0 | — |
| `iad` | Sterling (Virginia) | 4 | 27015–27140 | 4/4 | 101.7 ms |
| `lax` | Los Angeles (California) | 4 | 27015–27140 | 4/4 | 163.2 ms |
| `ord` | Chicago (Illinois) | 4 | 27015–27140 | 4/4 | 110.3 ms |
| `sea` | Seattle (Washington) | 2 | 27015–27140 | 2/2 | 153.1 ms |
| **South America** (4 POPs) | | | | | |
| `eze` | Buenos Aires (Argentina) | 4 | 27015–27060 | 4/4 | 254.2 ms |
| `gru` | Sao Paulo (Brazil) | 6 | 27015–27060 | 6/6 | 229.8 ms |
| `lim` | Lima (Peru) | 4 | 27015–27060 | 4/4 | 182.9 ms |
| `scl` | Santiago (Chile) | 2 | 27015–27060 | 2/2 | 216.2 ms |
| **Asia** (22 POPs) | | | | | |
| `bom2` | Mumbai (India) | 2 | 27015–27060 | 2/2 | 161.2 ms |
| `ctu` | Alibaba Cloud Chengdu (China) | 0 | — (no relays) | 0/0 | — |
| `ctum` | Alibaba Cloud Chengdu - Mobile (China) | 6 | 27015–27044 | 6/6 | 285.1 ms |
| `ctut` | Alibaba Cloud Chengdu - Telecom (China) | 5 | 27015–27044 | 5/5 | 214.7 ms |
| `ctuu` | Alibaba Cloud Chengdu - Unicom (China) | 4 | 27015–27044 | 4/4 | 196.0 ms |
| `hkg` | Hong Kong | 10 | 27015–27060 | 10/10 | 180.0 ms |
| `maa2` | Chennai - Ambattur (India) | 2 | 27015–27078 | 2/2 | 186.9 ms |
| `pek` | Alibaba Cloud Beijing (China) | 0 | — (no relays) | 0/0 | — |
| `pekm` | Alibaba Cloud Beijing - Mobile (China) | 5 | 27015–27044 | 5/5 | 246.4 ms |
| `pekt` | Alibaba Cloud Beijing - Telecom (China) | 5 | 27015–27044 | 5/5 | 174.0 ms |
| `peku` | Alibaba Cloud Beijing - Unicom (China) | 5 | 27015–27044 | 5/5 | 180.2 ms |
| `pvg` | Alibaba Cloud Shanghai (China) | 0 | — (no relays) | 0/0 | — |
| `pvgm` | Alibaba Cloud Shanghai - Mobile (China) | 8 | 27015–27044 | 8/8 | 260.9 ms |
| `pvgt` | Alibaba Cloud Shanghai - Telecom (China) | 8 | 27015–27044 | 8/8 | 193.9 ms |
| `pvgu` | Alibaba Cloud Shanghai - Unicom (China) | 8 | 27015–27044 | 8/8 | 207.0 ms |
| `seo` | Seoul (South Korea) | 4 | 27015–27060 | 4/4 | 304.2 ms |
| `sgp` | Singapore | 6 | 27015–27060 | 6/6 | 268.1 ms |
| `tgd` | Tencent Guangzhou (China) | 0 | — (no relays) | 0/0 | — |
| `tgdm` | Tencent Guangzhou - Mobile (China) | 5 | 27015–27044 | 5/5 | 215.3 ms |
| `tgdt` | Tencent Guangzhou - Telecom (China) | 5 | 27015–27044 | 5/5 | 188.4 ms |
| `tgdu` | Tencent Guangzhou - Unicom (China) | 5 | 27015–27044 | 5/5 | 164.8 ms |
| `tyo` | Tokyo Koto City (Japan) | 6 | 27015–27060 | 6/6 | 280.4 ms |
| **Oceania** (2 POPs) | | | | | |
| `gum` | Guam | 2 | 27015–27060 | 2/2 | 307.6 ms |
| `syd` | Sydney (Australia) | 4 | 27015–27140 | 3/4 | 306.4 ms |
| **Africa** (1 POP) | | | | | |
| `jnb` | Johannesburg (South Africa) | 2 | 27015–27060 | 2/2 | 195.8 ms |
| **Middle East** (2 POPs) | | | | | |
| `dfjr` | Datapacket Fujairah (UAE) | 2 | 27015–27060 | 2/2 | 145.4 ms |
| `dxb` | Dubai (United Arab Emirates) | 2 | 27015–27060 | 2/2 | 151.4 ms |

Region totals: Europe 15, North America 7, South America 4, Asia 22, Oceania 2,
Africa 1, Middle East 2, Unknown 0.

The union of all relay port ranges is **27015–27140**. The rule generator will derive
this union from the data (not hardcode it) and use it for every UDP drop rule.

Reproduce the table (and the anomaly checks) with:

```bash
python3 research/phase0_report.py --ping
```

---

## 3. Manual nftables test: block one real POP

### 3.1 What was tested

I created a temporary table `inet cs2blocker_phase0` (deliberately **not** `inet cs2blocker`,
so it cannot collide with the real tool) containing:

- a named counter `cnt_test`,
- a named set `pop_test` with one relay IP,
- one `output` chain at `priority filter; policy accept`,
- one UDP-only drop rule matching the SDR port range and the set.

The rule is exactly the shape the real tool will generate. The test was run inside an
unprivileged network namespace (`unshare -rn`), which is isolated from the host firewall.
That means it proves syntax, counter behavior, drop behavior and cleanup without needing
root or touching the host. It used `127.0.0.1` as the target so it could not affect real
traffic.

The actual host test is in §3.4 and has now passed on the real firewall.

### 3.2 Exact ruleset used

```nft
table inet cs2blocker_phase0 {
  counter cnt_test {
    comment "phase0 test drops for ams"
  }
  set pop_test {
    type ipv4_addr
    flags interval
    elements = { 155.133.248.36 comment "ams relay, phase0 test" }
  }
  chain output {
    type filter hook output priority filter; policy accept;
    udp dport 27015-27140 ip daddr @pop_test \
      counter name cnt_test drop comment "phase0 test: drop ams UDP"
  }
}
```

Checked before applying:

```bash
nft -c -f /tmp/cs2blocker-phase0.nft   # check only; no state change
nft -f   /tmp/cs2blocker-phase0.nft    # one transaction
```

Inspect and remove:

```bash
nft list table inet cs2blocker_phase0
nft delete table inet cs2blocker_phase0
```

No `flush ruleset`, no edit of `/etc/nftables.conf`, no other table is referenced.

### 3.3 Real observed output (isolated netns, target 127.0.0.1)

```text
== before: ICMP and UDP ==
2 packets transmitted, 2 received, 0% packet loss
UDP send -> 127.0.0.1:27015: allowed (3 datagrams queued)

== validate ruleset with nft --check (no changes) ==
check OK

== after: ICMP should still work, UDP should be dropped ==
2 packets transmitted, 2 received, 0% packet loss     <-- UDP-only rule, ICMP intact
UDP send -> 127.0.0.1:27015: BLOCKED (PermissionError: [Errno 1] Operation not permitted)

== counters ==
counter cnt_test {
    comment "phase0 test drops for ams"
    packets 1 bytes 51
}

== remove temporary table ==
UDP send -> 127.0.0.1:27015: allowed (3 datagrams queued)
table removed; UDP should work again
```

Two important observations:

1. **ICMP keeps working while UDP is blocked.** That is exactly what we want for the GUI
   ping column: if we blocked all protocols, the ping column would turn red for every
   blocked POP and become useless while queueing. It also limits collateral damage to
   other Valve services on the same IP.
2. **A netfilter `drop` in the `output` hook makes the local `sendto()` return `EPERM`**
   for a UDP socket. This is normal Linux behavior, not a bug: locally generated packets
   are dropped inside the send path, so the syscall reports the failure immediately. The
   game will see the relay as unreachable and should move on. This is the one behavior I
   want to validate with the real game in Phase 2; if CS2 mishandles `EPERM`, the
   alternative is an `egress` hook (silent drop) or `reject`, but that is a compatibility
   trade-off, not a blocker.

### 3.4 Run on the real host (passed)

The script is committed at `research/phase0-nft-test.sh`. It requires root, creates only
`inet cs2blocker_phase0`, puts all IPv4 relay addresses of the chosen POP into the test
set (the netns run above used a single loopback address instead), validates with
`nft -c`, exercises ping + UDP, prints the counter, and removes the table on exit
(including on failure).

```bash
cd /home/nasir/Dokumenter/Projects/CS2serverblocker
sudo research/phase0-nft-test.sh ams
```

You ran it on 2026-11-08 against the four Amsterdam relays and it passed exactly as
predicted:

```text
== target ==
POP=ams  UDP ports 27015-27140  (4 relay IPv4 addresses)
  relay: 155.133.248.36 / .37 / .40 / .41

== before ==   ping 2/2, 0% loss; UDP send allowed
== after ==    ping 2/2, 0% loss; UDP send BLOCKED (PermissionError: [Errno 1])
== counters == counter cnt_test { packets 1 bytes 51 }
== cleanup ==  UDP send allowed again; table removed
```

This confirms on the real host, not just in a namespace:

- the generated table and all four relay elements parse and apply;
- UDP to the SDR port range is dropped and the named counter increments;
- ICMP to the same relay still works (the UDP-only design keeps the ping column useful);
- cleanup is complete and `inet cs2blocker_phase0` is gone.

The `EPERM` is netfilter's normal response for locally generated packets dropped in the
`output` hook; it is not a script error. The remaining unknown is how the real CS2 client
reacts to that `EPERM` (Phase 2 test).

### 3.5 Atomicity evidence

`nft -f` applies a file as one transaction. I verified that a syntactically bad ruleset
which starts with `destroy table inet cs2blocker` does **not** replace the existing table:

```text
$ nft -f /tmp/bad.nft
/tmp/bad.nft:5:23-26: Error: syntax error, unexpected drop
$ nft list table inet cs2blocker >/dev/null && echo "previous table still present"
previous table still present
```

This is the fail-safe property the helper relies on: generate the complete text, run
`nft -c`, then apply once. A failed apply leaves the previous rules in place.

One version caveat found during research:

- `destroy table` (delete-if-exists) is the clean way to make apply idempotent. It was
  added in nftables **1.0.7** (released March 2023; commit in February). Debian 12 ships
  1.0.6, Ubuntu 22.04 ships 1.0.2.
- On older nft, `delete table` fails if the table does not exist, aborting the whole
  transaction. The helper must therefore either use `destroy` when the nft version
  supports it, or check table existence first and emit `delete table` only when needed.
  Both paths still use **one** `nft -f` transaction for delete+create. This will be
  covered by unit tests and by a version-capability check.

---

## 4. ICMP behavior and the ping column

### 4.1 Do relays answer ICMP?

Yes, overwhelmingly. I sent one unprivileged ICMP echo to each of the 220 relay IPs:

```text
219 / 220 replied (1 s timeout)
```

The one miss was a single Sydney relay (`syd` still had 3/4 replies); it may be rate
limiting or filtering ICMP. Some of the China relays have high or variable RTT, but they
answered.

### 4.2 Unprivileged ICMP works on this host

```console
$ sysctl net.ipv4.ping_group_range
net.ipv4.ping_group_range = 0    2147483647
$ getcap /usr/bin/ping        # empty; not setuid either
$ ls -l /usr/bin/ping
-rwxr-xr-x 1 root root 159288 ...
```

So `ping` works through the kernel's unprivileged ICMP datagram support, and so does
this pure-Python socket:

```python
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
s.settimeout(1.0)
s.sendto(echo_request_without_checksum, (ip, 0))   # kernel fills the checksum
data, addr = s.recvfrom(1500)
```

No root, no `CAP_NET_RAW`, no setuid binary. This is what the GUI will use for the live
ping column.

### 4.3 Fallback for systems where that is restricted

`net.ipv4.ping_group_range` can be narrowed by the admin (some hardened distros do).
The ping layer will then:

1. try the unprivileged datagram socket;
2. on `EPERM`, fall back to the `ping` binary (`ping -n -c1 -W1 <ip>`), which may be
   setuid or carry `cap_net_raw`;
3. if neither works, show `n/a` with a tooltip explaining that ICMP is unavailable and
   that the game's own UDP measurements are unaffected.

The GUI must never hang on ping: it will use a bounded thread pool, per-request timeouts,
and `GLib.idle_add` to update rows on the main loop. Blocking the UI while pinging 220
relays would be the classic "it never worked" failure.

**Caveat:** ICMP RTT is informative, not authoritative. The game selects POPs using SDR
UDP measurements. A POP can have good ICMP and bad SDR (or vice versa), and some
networks filter ICMP entirely. The column will be labelled as a hint, not a promise.

---

## 5. All protocols vs UDP-only: recommendation

**Recommendation: block UDP only, to the relay IPs, on the SDR port range
(union 27015–27140), in the `output` hook. Default `protocol = "udp"`; offer
`protocol = "all"` as an advanced setting.**

Why UDP-only is the right default:

1. **That is the traffic that matters.** SDR game traffic and SDR's own relay
   measurements are UDP. Dropping UDP to the relay makes that POP unusable for
   matchmaking, which is the goal.
2. **ICMP stays available**, so the GUI ping column keeps working while a POP is
   blocked (verified in §3.3). It also means the user can still diagnose connectivity.
3. **Less collateral damage.** These are shared Valve anycast addresses. TCP/ICMP or
   UDP outside the SDR range may belong to Steam downloads, voice, other Valve games, or
   unrelated services. Blocking only SDR UDP ports is the narrowest effective rule.
4. **Counters are meaningful.** Every counter increment corresponds to game-related
   relay UDP, not to unrelated traffic.

Why not block all protocols:

- It works, and it is what some Windows firewall tools do, but it is broader than
  necessary and it kills the ping column.
- It may break other Valve games/services sharing the IP, and it makes diagnostics
  harder.
- It does not improve matchmaking avoidance: the game already treats a UDP-dead POP as
  unusable.

Caveats to validate in Phase 1/2:

- If Valve introduces a TCP fallback for SDR (none observed), `udp` would not cover it.
  The `all` setting is the escape hatch.
- The `EPERM` behavior in §3.3 must be checked against the real CS2 client. If the game
  reacts badly, the fallback is a different drop point (`egress` hook or `reject`), not
  "block all protocols".
- The block is on the host's network stack. Flatpak Steam shares the host network
  namespace by default, so this catches it; a game inside a VM/container would need the
  optional `forward` chain. Native vs Flatpak Steam is otherwise irrelevant.
- No conntrack flushing is needed or wanted: dropping output packets is sufficient, and
  `conntrack -F` would be a system-wide destructive action.

---

## 6. The CLI tool you found

**I have not received it yet** — the workspace is empty, and it was not attached to the
prompt. Paste it (or give me the URL) and I will review the actual code before Phase 1.
I am not going to pretend to review code I have not seen.

Generic pitfalls I will be checking for, based on the failure modes described in your
brief. These are also the traps the new tool must avoid:

| pitfall | why it bites | how the new design avoids it |
|---|---|---|
| Hardcoded relay IPs | Valve changes relays/POPs without warning; IPs go stale | fetch weekly + on demand; atomic root-owned cache; bundled fallback only for first run |
| No refresh, or refresh overwrites good data on failure | a transient 500/network outage leaves you with no rules or half a list | fetch into memory, validate fully, atomic replace only on success; keep last good cache |
| Blocking one POP by IP only | IPs are shared/anycast in the future; blocking an IP can affect another POP | per-POP sets + overlap detection; warn and exclude overlapping IPs in allow-only mode |
| `nft flush ruleset` or editing `/etc/nftables.conf` | wipes ufw/firewalld/Docker rules and the user's own firewall | only `inet cs2blocker`; `destroy/delete table` of that table only |
| Rules lost on reboot | nftables state is not persistent | systemd oneshot `After=network-pre.target`, `RemainAfterExit=yes`; refresh timer |
| Half-applied rules | a script applying rule-by-rule can die in the middle | one generated file, `nft -c` then one `nft -f` transaction |
| Root helper accepts raw IPs/config paths | local privilege escalation / arbitrary firewall writes | helper accepts only POP codes + settings; builds from its own root-owned cache; strict validation |
| User-writable config | the user (or malware as the user) can inject rules | `/etc/cs2blocker/config.json` root-owned 0644; writes only via helper |
| Conflicts with ufw/firewalld/Docker | other managers reload/flush their own tables | our own `inet` table; ordering after `nftables.service`/`ufw.service`; doctor checks |
| Ping via raw sockets | needs root; GUI freezes on 220 timeouts | unprivileged ICMP socket + thread pool + fallback; ICMP is optional |
| Polkit assumptions | minimal WMs have no agent; pkexec fails silently | detect, show the exact `sudo cs2blockctl ...` fallback |
| No counters | cannot tell whether blocking works while queueing | named per-POP counters + a small root monitor that publishes counters read-only |
| "Unblock" missing or partial | rules survive and the user cannot undo | `cs2blockctl clear` + uninstall script; table deletion is idempotent |
| IPv6 blindness | future relays may be IPv6-only | parse `ipv6`, generate `ip6` sets/rules; currently zero IPv6 data |
| Steam/Flatpak path assumptions | the firewall does not care where Steam lives | host-level `output` chain; document that game files are untouched |

---

## 7. Design summary

The full design is in [design.md](design.md). The short version:

- **One nftables table** `inet cs2blocker`, one `output` chain (optional `forward`),
  one named set and one named counter per POP, one UDP drop rule per set.
- **One root helper**, reached through a polkit action with `auth_admin_keep`, or run by
  systemd. It accepts only POP codes and settings; it never accepts IPs or file paths.
- **Root-owned data**: `/var/lib/cs2blocker/sdr-730.json` (fetched by the helper),
  `/etc/cs2blocker/config.json` (selection/presets), `/run/cs2blocker/status.json`
  (read-only status/counters for the GUI).
- **Boot persistence**: `cs2blocker.service` (oneshot, after `network-pre.target`) and
  `cs2blocker-refresh.timer` (weekly, randomized, re-fetch + re-apply).
- **GUI**: GTK4/libadwaita via PyGObject (already installed here; native Wayland;
  no Qt platform-theme dependency). The GUI never runs as root; it uses `pkexec` only
  for Apply / Unblock / persistence changes, and reads status/counters from world-readable
  files.
- **Live counters without repeated password prompts**: a tiny root
  `cs2blocker-monitor.service` reads `nft -j` every ~2 s and atomically publishes
  `/run/cs2blocker/status.json` (0644). This is an addition to the Phase 2 unit list;
  flag it if you would rather avoid the daemon and accept counters only after Apply.
- **Modes**: `Block selected` and `Allow only selected`. In allow-only mode every
  non-selected POP is blocked; new POPs in a refreshed list are blocked by default.
  Region presets expand to explicit POP codes at apply time, so a new POP does not
  silently become allowed.
- **Fail safe**: failed fetch keeps the cache; failed apply leaves the old table; every
  action is reversible with `cs2blockctl clear` or `uninstall.sh`.

---

## 8. What I need from you before Phase 1

1. **Paste the CLI tool** you mentioned (or a link). I will read it and give you a real
   review instead of the generic pitfall list above.
2. **Host test:** done — the 2026-11-08 run against all four Amsterdam relays passed
   and is recorded in §3.4. No action needed unless you want to repeat it.
3. **Approve the design** in [design.md](design.md), especially:
   - GTK4/libadwaita for the GUI (you are on KDE Plasma Wayland; I chose it for
     installed-dependency and Wayland reliability, not KDE-native looks — say if you
     prefer Qt6/PySide6 and I will switch before Phase 4);
   - UDP-only as the default block protocol;
   - the `cs2blocker-monitor.service` addition for live counters.
4. **Confirm your desktop** (detected: KDE Plasma, Wayland) and whether Steam is native
   or Flatpak (only relevant to documentation; blocking is system-level either way).
5. Anything else in the brief you want changed before I write code? In particular, the
   Phase 7 items are still explicitly optional and I will not start them without a yes.
