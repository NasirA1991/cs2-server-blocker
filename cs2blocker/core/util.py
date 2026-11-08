"""Small, dependency-free helpers shared by the core, helper and CLI."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

FIXED_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
POP_CODE_RE = re.compile(r"^[a-z0-9]{2,12}$")
PRESET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class Cs2BlockerError(RuntimeError):
    """Base class for expected, user-facing failures."""


class ValidationError(Cs2BlockerError):
    pass


class BackendError(Cs2BlockerError):
    pass


class ConfigError(Cs2BlockerError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(timestamp: str | None) -> float | None:
    parsed = parse_utc(timestamp)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def human_age(timestamp: str | None) -> str:
    seconds = age_seconds(timestamp)
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 90 * 60:
        return f"{int(seconds / 60)}m ago"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def sanitize_text(value: object, *, max_len: int = 120) -> str:
    """Collapse control characters/whitespace and cap a display string."""
    text = str(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    return text[:max_len].strip()


def nft_comment(value: object, *, max_len: int = 120) -> str:
    """Return a string safe to embed inside a double-quoted nft comment."""
    text = sanitize_text(value, max_len=max_len)
    text = text.replace("\\", "/").replace('"', "'")
    # Keep comments ASCII so every nft version/terminal renders them the same.
    text = text.encode("ascii", "replace").decode("ascii")
    return text or "cs2blocker"


def validate_pop_code(value: str) -> str:
    code = str(value).strip().lower()
    if not POP_CODE_RE.fullmatch(code):
        raise ValidationError(f"invalid POP code: {value!r}")
    return code


def parse_pop_codes(values: str | Iterable[str]) -> frozenset[str]:
    if isinstance(values, str):
        raw = re.split(r"[\s,]+", values.strip())
    else:
        raw = []
        for item in values:
            raw.extend(re.split(r"[\s,]+", str(item).strip()))
    codes: set[str] = set()
    for item in raw:
        if not item:
            continue
        codes.add(validate_pop_code(item))
    return frozenset(codes)


def validate_preset_key(value: str) -> str:
    key = str(value).strip().lower().replace(" ", "_")
    if not PRESET_KEY_RE.fullmatch(key):
        raise ValidationError(
            "preset name must be 1-32 lowercase letters, digits, '-' or '_'"
        )
    return key


_PUBLIC_REJECT_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)
_PUBLIC_REJECT_V6 = (
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("2001:db8::/32"),
)


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global:
        return False
    reject = _PUBLIC_REJECT_V4 if address.version == 4 else _PUBLIC_REJECT_V6
    return not any(address in network for network in reject)


def validate_relay_address(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError as exc:
        raise ValidationError(f"invalid relay address: {value!r}") from exc
    if not is_public_address(address):
        raise ValidationError(f"refusing non-public relay address: {address}")
    return address


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    cleaned: list[tuple[int, int]] = []
    for start, end in ranges:
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValidationError("port range bounds must be integers")
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValidationError("port range bounds must not be booleans")
        if not (1 <= start <= end <= 65535):
            raise ValidationError(f"invalid port range: {start}-{end}")
        cleaned.append((start, end))
    if not cleaned:
        return ()
    cleaned.sort()
    merged: list[tuple[int, int]] = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def format_ranges(ranges: Sequence[tuple[int, int]]) -> str:
    return ", ".join(
        str(start) if start == end else f"{start}-{end}" for start, end in ranges
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug[:32] or "preset"


def which_fixed(name: str) -> str | None:
    return shutil.which(name, path=FIXED_PATH)


def command_exists(name: str) -> bool:
    return which_fixed(name) is not None


def run(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float = 60.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell, with a fixed sane environment."""
    clean_env = {
        "PATH": FIXED_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/root",
    }
    if env:
        clean_env.update(env)
    try:
        return subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=clean_env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackendError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"command timed out: {' '.join(map(str, argv))}") from exc


def require_root() -> None:
    if os.geteuid() != 0:
        raise Cs2BlockerError("this operation requires root (use sudo or pkexec)")


def sleep(seconds: float) -> None:
    time.sleep(seconds)
