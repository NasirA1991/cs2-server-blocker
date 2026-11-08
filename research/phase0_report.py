#!/usr/bin/env python3
"""Phase 0 evidence collector for the CS2 SDR config (appid 730).

This is research tooling, not the application.  It fetches the SDR config,
checks the schema we actually depend on, and prints a Markdown report:
schema summary, POPs grouped by region, and data anomalies.

Optionally probes every relay with a single unprivileged ICMP echo.

    python3 research/phase0_report.py                 # use saved snapshot
    python3 research/phase0_report.py --fetch         # refresh snapshot
    python3 research/phase0_report.py --ping          # add ICMP column
"""

from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import socket
import struct
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

URL = "https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/?appid=730"
SNAPSHOT = Path(__file__).with_name("sdr-730-20261108.json")

# Valve's config has no region field.  Region must be curated by POP code.
# Aliases are mapped to their canonical POP and never appear as keys in "pops".
REGION_BY_POP: dict[str, str] = {
    # Europe
    "ams": "Europe",
    "dfra": "Europe",
    "dlhr": "Europe",
    "dsto": "Europe",
    "dvie": "Europe",
    "fsn": "Europe",
    "fra": "Europe",
    "hel": "Europe",
    "lhr": "Europe",
    "mad": "Europe",
    "par": "Europe",
    "sto": "Europe",
    "sto2": "Europe",
    "vie": "Europe",
    "waw": "Europe",
    # North America
    "atl": "North America",
    "dfw": "North America",
    "eat": "North America",
    "iad": "North America",
    "lax": "North America",
    "ord": "North America",
    "sea": "North America",
    # Asia
    "bom2": "Asia",
    "maa2": "Asia",
    "ctu": "Asia",
    "ctum": "Asia",
    "ctut": "Asia",
    "ctuu": "Asia",
    "pek": "Asia",
    "pekm": "Asia",
    "pekt": "Asia",
    "peku": "Asia",
    "pvg": "Asia",
    "pvgm": "Asia",
    "pvgt": "Asia",
    "pvgu": "Asia",
    "tgd": "Asia",
    "tgdm": "Asia",
    "tgdt": "Asia",
    "tgdu": "Asia",
    "hkg": "Asia",
    "sgp": "Asia",
    "seo": "Asia",
    "tyo": "Asia",
    # South America
    "eze": "South America",
    "gru": "South America",
    "lim": "South America",
    "scl": "South America",
    # Oceania
    "gum": "Oceania",
    "syd": "Oceania",
    # Africa
    "jnb": "Africa",
    # Middle East
    "dfjr": "Middle East",
    "dxb": "Middle East",
}
REGION_ORDER = [
    "Europe",
    "North America",
    "South America",
    "Asia",
    "Oceania",
    "Africa",
    "Middle East",
    "Unknown",
]


@dataclass
class RelayInfo:
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address
    start: int
    end: int


def load(path: Path) -> dict:
    with path.open("rb") as fh:
        return json.load(fh)


def fetch() -> dict:
    req = urllib.request.Request(URL, headers={"User-Agent": "cs2blocker-phase0/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.geturl() != URL:
            raise RuntimeError(f"unexpected redirect: {resp.geturl()}")
        body = resp.read(4_000_000 + 1)
    if len(body) > 4_000_000:
        raise RuntimeError("response too large")
    return json.loads(body)


def validate(doc: dict) -> None:
    if doc.get("success") is not True:
        raise ValueError(f"success != true: {doc.get('message')!r}")
    pops = doc.get("pops")
    if not isinstance(pops, dict) or not pops:
        raise ValueError("pops missing/empty")
    for code, pop in pops.items():
        if not isinstance(code, str) or not code.isalnum() or code != code.lower():
            raise ValueError(f"bad POP code: {code!r}")
        if not isinstance(pop.get("desc"), str):
            raise ValueError(f"{code}: bad desc")
        geo = pop.get("geo")
        if not (isinstance(geo, list) and len(geo) == 2):
            raise ValueError(f"{code}: bad geo")
        for relay in pop.get("relays", []):
            key = "ipv4" if "ipv4" in relay else "ipv6" if "ipv6" in relay else None
            if key is None:
                raise ValueError(f"{code}: relay has no address")
            addr = ipaddress.ip_address(relay[key])
            if not addr.is_global:
                raise ValueError(f"{code}: non-global relay {addr}")
            start, end = relay["port_range"]
            if not (1 <= start <= end <= 65535):
                raise ValueError(f"{code}: bad port range {start}-{end}")


def relays(pop: dict) -> list[RelayInfo]:
    out = []
    for r in pop.get("relays", []):
        key = "ipv4" if "ipv4" in r else "ipv6"
        out.append(RelayInfo(ipaddress.ip_address(r[key]), *r["port_range"]))
    return out


def ping(addr: str, timeout: float = 1.0) -> float | None:
    """One ICMP echo using an unprivileged SOCK_DGRAM/IPPROTO_ICMP socket."""
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    proto = socket.IPPROTO_ICMPV6 if family == socket.AF_INET6 else socket.IPPROTO_ICMP
    pkt = struct.pack("!BBHHH", 128 if family == socket.AF_INET6 else 8, 0, 0, 0, 1)
    try:
        with socket.socket(family, socket.SOCK_DGRAM, proto) as sock:
            sock.settimeout(timeout)
            started = time.perf_counter()
            sock.sendto(pkt + b"cs2blocker", (addr, 0))
            data, _ = sock.recvfrom(1500)
            elapsed = (time.perf_counter() - started) * 1000
        if data[0] not in (0, 129):
            return None
        return elapsed
    except OSError:
        return None


def md_table(doc: dict, pings: dict[str, float | None] | None) -> str:
    pops = doc["pops"]
    by_region: dict[str, list[tuple[str, dict]]] = collections.defaultdict(list)
    for code, pop in pops.items():
        by_region[REGION_BY_POP.get(code, "Unknown")].append((code, pop))

    lines = []
    header = "| POP | Location | Relays | Port range(s) |"
    sep = "|---|---|---:|---|"
    if pings is not None:
        header += " ICMP replies | min RTT |"
        sep += "---|---:|"
    lines += [header, sep]
    for region in REGION_ORDER:
        rows = sorted(by_region.get(region, []), key=lambda item: item[0])
        if not rows:
            continue
        label = "POP" if len(rows) == 1 else "POPs"
        lines.append(f"| **{region}** ({len(rows)} {label}) | | | |" + (" | |" if pings else ""))
        for code, pop in rows:
            rl = relays(pop)
            ports = sorted({(r.start, r.end) for r in rl})
            if ports:
                port_s = ", ".join(f"{a}–{b}" if a != b else str(a) for a, b in ports)
            else:
                port_s = "— (no relays)"
            row = f"| `{code}` | {pop['desc']} | {len(rl)} | {port_s} |"
            if pings is not None:
                vals = [pings.get(str(r.addr)) for r in rl]
                ok = sum(1 for v in vals if v is not None)
                best = f"{min(v for v in vals if v is not None):.1f} ms" if ok else "—"
                row += f" {ok}/{len(rl)} | {best} |"
            lines.append(row)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="fetch a fresh copy")
    ap.add_argument("--ping", action="store_true", help="probe every relay with ICMP")
    ap.add_argument("--json-out", type=Path, help="write raw JSON here")
    args = ap.parse_args()

    doc = fetch() if args.fetch else load(SNAPSHOT)
    validate(doc)

    pops = doc["pops"]
    all_relays = [r for pop in pops.values() for r in relays(pop)]
    v6 = sum(1 for r in all_relays if r.addr.version == 6)
    port_ranges = collections.Counter((r.start, r.end) for r in all_relays)
    relay_keys = collections.Counter(
        key for pop in pops.values() for r in pop.get("relays", []) for key in r
    )

    print(f"# CS2 SDR config, appid 730\n")
    print(f"- revision: `{doc.get('revision')}`")
    print(f"- POPs: {len(pops)} ({sum(1 for p in pops.values() if not p.get('relays'))} without relays)")
    print(f"- relay endpoints: {len(all_relays)} (IPv6: {v6})")
    print(f"- relay fields: {dict(relay_keys)}")
    print(f"- port ranges: {dict(port_ranges)}")
    print(f"- top-level keys: {', '.join(sorted(doc))}\n")

    empty = [code for code, pop in sorted(pops.items()) if not pop.get("relays")]
    print(f"POPs without relays ({len(empty)}): {', '.join(empty)}\n")

    ip_to_pops: dict[str, set[str]] = collections.defaultdict(set)
    for code, pop in pops.items():
        for r in relays(pop):
            ip_to_pops[str(r.addr)].add(code)
    shared = {ip: cs for ip, cs in ip_to_pops.items() if len(cs) > 1}
    print(f"Relay IPs shared between POPs: {len(shared)}")
    for ip, cs in sorted(shared.items()):
        print(f"  - {ip}: {', '.join(sorted(cs))}")
    print()

    print("Note: `geo` is [longitude, latitude]; `desc` is the only location label.\n")

    pings = None
    if args.ping:
        pings = {}
        work = [(str(r.addr)) for r in all_relays]
        with ThreadPoolExecutor(max_workers=64) as pool:
            results = list(pool.map(ping, work))
        pings = dict(zip(work, results))
        ok = sum(1 for v in results if v is not None)
        print(f"ICMP: {ok}/{len(results)} relays answered one echo (1 s timeout)\n")

    print(md_table(doc, pings))

    if args.json_out:
        args.json_out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print(f"\nRaw JSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
