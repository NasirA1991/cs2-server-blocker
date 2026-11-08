"""Unprivileged ICMP ping helpers used by the CLI and GUI.

Linux allows ``SOCK_DGRAM/IPPROTO_ICMP`` without root when
``net.ipv4.ping_group_range`` includes the caller's group.  If that fails we
fall back to the ``ping`` binary, which is setuid or file-capped on many
distributions.  ICMP is only a hint: SDR uses its own UDP measurements.
"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable

_TIME_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")


@dataclass(frozen=True, slots=True)
class PingResult:
    address: str
    rtt_ms: float | None
    method: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.rtt_ms is not None


def _icmp_socket(address: str, timeout: float) -> socket.socket:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    proto = socket.IPPROTO_ICMPV6 if family == socket.AF_INET6 else socket.IPPROTO_ICMP
    sock = socket.socket(family, socket.SOCK_DGRAM, proto)
    sock.settimeout(timeout)
    return sock


def ping(address: str, timeout: float = 1.0) -> PingResult:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    icmp_type = 128 if family == socket.AF_INET6 else 8
    reply_type = 129 if family == socket.AF_INET6 else 0
    packet = struct.pack("!BBHHH", icmp_type, 0, 0, 0xA55A, 1) + b"cs2blocker"
    try:
        with _icmp_socket(address, timeout) as sock:
            started = time.perf_counter()
            sock.sendto(packet, (address, 0))
            data, _ = sock.recvfrom(1500)
            elapsed = (time.perf_counter() - started) * 1000.0
        if not data or data[0] != reply_type:
            return PingResult(address, None, "icmp", "unexpected ICMP reply")
        return PingResult(address, elapsed, "icmp")
    except PermissionError:
        return _ping_binary(address, timeout)
    except OSError as exc:
        return PingResult(address, None, "icmp", str(exc))


def _ping_binary(address: str, timeout: float) -> PingResult:
    binary = "ping"
    command = [binary, "-n", "-c", "1", "-W", str(max(1, int(round(timeout)))), address]
    if ":" in address:
        command.insert(1, "-6")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 3.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return PingResult(address, None, "ping", str(exc))
    match = _TIME_RE.search(result.stdout)
    if result.returncode == 0 and match:
        return PingResult(address, float(match.group(1)), "ping")
    error = (result.stderr or result.stdout).strip().splitlines()
    return PingResult(address, None, "ping", error[-1] if error else "no reply")


def ping_many(addresses: Iterable[str], *, timeout: float = 1.0, workers: int = 32) -> dict[str, PingResult]:
    from concurrent.futures import ThreadPoolExecutor

    unique = list(dict.fromkeys(addresses))
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda addr: ping(addr, timeout), unique))
    return dict(zip(unique, results))


def pop_ping_target(relays) -> str | None:
    for relay in relays:
        if relay.version == 4:
            return relay.address
    for relay in relays:
        if relay.version == 6:
            return relay.address
    return None


def rtt_text(result: PingResult | None) -> str:
    if result is None:
        return "—"
    if result.rtt_ms is None:
        return "n/a"
    return f"{result.rtt_ms:.0f} ms"
