"""Fetch, validate, normalize and cache Valve's SDR configuration.

The document is untrusted input even though it comes over TLS: every address,
port range, POP code and string is validated before it can reach the firewall.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import regions
from .model import Pop, Relay, SdrSnapshot, SnapshotDiff
from .state import read_json, write_json
from .util import (
    Cs2BlockerError,
    ValidationError,
    sanitize_text,
    utc_now,
    validate_pop_code,
    validate_relay_address,
)

SDR_URL = "https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/?appid={appid}"
SDR_HOST = "api.steampowered.com"
MAX_RESPONSE_BYTES = 4_000_000
MAX_POPS = 200
MAX_RELAYS_PER_POP = 64
MAX_TOTAL_RELAYS = 5000
MAX_PORT_SPAN = 1024
CACHE_SCHEMA = 1


class SdrError(Cs2BlockerError):
    pass


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != SDR_HOST:
            raise SdrError(f"refusing SDR redirect to untrusted URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_AllowlistRedirect())


def fetch_raw(appid: int = 730, *, timeout: float = 15.0) -> bytes:
    if not isinstance(appid, int) or isinstance(appid, bool) or not (1 <= appid <= 10**9):
        raise SdrError(f"invalid appid: {appid!r}")
    url = SDR_URL.format(appid=appid)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "cs2blocker/0.1 (+https://github.com/cs2blocker)",
            "Accept": "application/json",
        },
    )
    try:
        with _opener().open(request, timeout=timeout) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != SDR_HOST:
                raise SdrError(f"unexpected SDR response URL: {response.geturl()}")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace").strip()
        except Exception:
            pass
        raise SdrError(f"SDR endpoint returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SdrError(f"cannot reach SDR endpoint: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SdrError("SDR endpoint timed out") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise SdrError("SDR response is too large")
    if not body:
        raise SdrError("SDR endpoint returned an empty body")
    return body


def _parse_geo(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lon = float(value[0])
        lat = float(value[1])
    except (TypeError, ValueError):
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return (lon, lat)


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_port_range(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValidationError(f"invalid port_range: {value!r}")
    start, end = value
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValidationError(f"invalid port_range: {value!r}")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValidationError(f"invalid port_range: {value!r}")
    if not (1 <= start <= end <= 65535):
        raise ValidationError(f"invalid port_range: {value!r}")
    if end - start > MAX_PORT_SPAN:
        raise ValidationError(f"port_range spans too many ports: {start}-{end}")
    return start, end


def _parse_aliases(value: Any, code: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value[:16]:
        try:
            alias = validate_pop_code(str(item))
        except ValidationError:
            continue
        if alias != code and alias not in out:
            out.append(alias)
    return tuple(out)


def parse_valve(
    document: Mapping[str, Any],
    *,
    appid: int = 730,
    fetched_at: str | None = None,
    source: str = "network",
) -> SdrSnapshot:
    if not isinstance(document, Mapping):
        raise SdrError("SDR document is not a JSON object")
    if document.get("success") is not True:
        message = sanitize_text(document.get("message", "unknown error"), max_len=200)
        raise SdrError(f"SDR endpoint reported failure: {message}")
    pops_raw = document.get("pops")
    if not isinstance(pops_raw, Mapping) or not pops_raw:
        raise SdrError("SDR document has no POPs")
    if len(pops_raw) > MAX_POPS:
        raise SdrError(f"SDR document has too many POPs ({len(pops_raw)})")
    revision_raw = document.get("revision", 0)
    revision = 0 if revision_raw is None else revision_raw
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise SdrError(f"invalid SDR revision: {revision_raw!r}")
    timestamp = fetched_at or utc_now()
    source_name = sanitize_text(source, max_len=32) or "unknown"

    pops: dict[str, Pop] = {}
    total_relays = 0
    for raw_code, raw_pop in pops_raw.items():
        code = validate_pop_code(str(raw_code))
        if not isinstance(raw_pop, Mapping):
            raise SdrError(f"POP {code} is not an object")
        name = sanitize_text(raw_pop.get("desc") or code, max_len=120) or code
        relays_raw = raw_pop.get("relays", [])
        if relays_raw is None:
            relays_raw = []
        if not isinstance(relays_raw, list):
            raise SdrError(f"POP {code} relays is not a list")
        if len(relays_raw) > MAX_RELAYS_PER_POP:
            raise SdrError(f"POP {code} has too many relays")
        relays: list[Relay] = []
        seen_addresses: set[str] = set()
        for raw_relay in relays_raw:
            if not isinstance(raw_relay, Mapping):
                raise SdrError(f"POP {code} contains a malformed relay")
            has_v4 = "ipv4" in raw_relay and raw_relay["ipv4"] is not None
            has_v6 = "ipv6" in raw_relay and raw_relay["ipv6"] is not None
            if has_v4 == has_v6:
                raise SdrError(f"POP {code} relay must have exactly one address family")
            key = "ipv4" if has_v4 else "ipv6"
            try:
                address = validate_relay_address(raw_relay[key])
                start, end = _parse_port_range(raw_relay.get("port_range"))
            except ValidationError as exc:
                raise SdrError(f"POP {code}: {exc}") from exc
            address_text = str(address)
            if address_text in seen_addresses:
                # Same IP is occasionally repeated with a different port range.
                # The firewall set only stores the address; keep the first one.
                continue
            seen_addresses.add(address_text)
            relays.append(Relay(address_text, start, end))
            total_relays += 1
            if total_relays > MAX_TOTAL_RELAYS:
                raise SdrError("SDR document has too many relay endpoints")
        relays.sort(key=lambda r: (r.version, int(ipaddress.ip_address(r.address)), r.port_start))
        pops[code] = Pop(
            code=code,
            name=name,
            region=regions.region_for(code),
            relays=tuple(relays),
            geo=_parse_geo(raw_pop.get("geo")),
            tier=_parse_int(raw_pop.get("tier")),
            aliases=_parse_aliases(raw_pop.get("aliases"), code),
        )
    return SdrSnapshot(
        appid=int(appid),
        revision=revision,
        fetched_at=timestamp,
        source=source_name,
        pops=pops,
    )


def parse_raw(raw: bytes, *, appid: int = 730, source: str = "network") -> SdrSnapshot:
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SdrError(f"invalid JSON from SDR endpoint: {exc}") from exc
    return parse_valve(document, appid=appid, source=source)


def snapshot_to_dict(snapshot: SdrSnapshot) -> dict[str, Any]:
    pops: dict[str, Any] = {}
    for code in sorted(snapshot.pops):
        pop = snapshot.pops[code]
        pops[code] = {
            "desc": pop.name,
            "region": pop.region.value,
            "geo": list(pop.geo) if pop.geo is not None else None,
            "tier": pop.tier,
            "aliases": list(pop.aliases),
            "relays": [
                {"address": relay.address, "port_range": [relay.port_start, relay.port_end]}
                for relay in pop.relays
            ],
        }
    return {
        "schema": CACHE_SCHEMA,
        "appid": snapshot.appid,
        "revision": snapshot.revision,
        "fetched_at": snapshot.fetched_at,
        "source": snapshot.source,
        "pops": pops,
    }


def snapshot_from_dict(document: Any, *, appid: int = 730) -> SdrSnapshot:
    if not isinstance(document, Mapping):
        raise SdrError("cached SDR document is not an object")
    schema = document.get("schema")
    if schema != CACHE_SCHEMA:
        raise SdrError(f"unsupported cache schema: {schema!r}")
    cached_appid = document.get("appid")
    if cached_appid is not None and int(cached_appid) != int(appid):
        raise SdrError(f"cache is for appid {cached_appid}, expected {appid}")
    raw_pops = document.get("pops")
    if not isinstance(raw_pops, Mapping):
        raise SdrError("cached SDR document has no POPs")
    valve_pops: dict[str, Any] = {}
    for code, raw_pop in raw_pops.items():
        if not isinstance(raw_pop, Mapping):
            raise SdrError(f"cached POP {code} is malformed")
        relays: list[dict[str, Any]] = []
        for raw_relay in raw_pop.get("relays", []) or []:
            if not isinstance(raw_relay, Mapping):
                raise SdrError(f"cached relay in {code} is malformed")
            address = str(raw_relay.get("address", ""))
            key = "ipv6" if ":" in address else "ipv4"
            relays.append(
                {
                    key: address,
                    "port_range": raw_relay.get("port_range", []),
                }
            )
        valve_pops[str(code)] = {
            "desc": raw_pop.get("desc", code),
            "geo": raw_pop.get("geo"),
            "tier": raw_pop.get("tier"),
            "aliases": raw_pop.get("aliases", []),
            "relays": relays,
        }
    return parse_valve(
        {
            "success": True,
            "revision": document.get("revision", 0),
            "pops": valve_pops,
        },
        appid=appid,
        fetched_at=str(document.get("fetched_at") or utc_now()),
        source=str(document.get("source") or "cache"),
    )


def load_snapshot_file(path: Path, *, appid: int = 730, require_root: bool = True) -> SdrSnapshot:
    document = read_json(path, require_root=require_root)
    return snapshot_from_dict(document, appid=appid)


def save_snapshot_file(path: Path, snapshot: SdrSnapshot) -> None:
    write_json(path, snapshot_to_dict(snapshot), mode=0o644)


def load_bundled(appid: int = 730) -> SdrSnapshot:
    from .state import Paths

    path = Paths.bundled_snapshot(appid)
    if not path.is_file():
        raise SdrError(f"bundled SDR snapshot is missing: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    return snapshot_from_dict(document, appid=appid)


def diff_snapshots(old: SdrSnapshot | None, new: SdrSnapshot) -> SnapshotDiff:
    if old is None:
        return SnapshotDiff(
            changed=True,
            revision_changed=True,
            added_addresses=new.relay_addresses(),
            added_pops=new.pop_codes(),
        )
    old_addresses = old.relay_addresses()
    new_addresses = new.relay_addresses()
    old_pops = old.pop_codes()
    new_pops = new.pop_codes()
    revision_changed = old.revision != new.revision
    changed = (
        revision_changed
        or old_addresses != new_addresses
        or old_pops != new_pops
    )
    return SnapshotDiff(
        changed=changed,
        revision_changed=revision_changed,
        added_addresses=frozenset(new_addresses - old_addresses),
        removed_addresses=frozenset(old_addresses - new_addresses),
        added_pops=frozenset(new_pops - old_pops),
        removed_pops=frozenset(old_pops - new_pops),
    )


def config_age_hours(snapshot: SdrSnapshot) -> float | None:
    from .util import age_seconds

    seconds = age_seconds(snapshot.fetched_at)
    if seconds is None:
        return None
    return seconds / 3600.0
