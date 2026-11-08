from __future__ import annotations

from typing import Any

from cs2blocker.core.sdr import parse_valve


def raw_doc(pops: dict[str, Any], *, success: bool = True, revision: int = 1) -> dict[str, Any]:
    return {"success": success, "revision": revision, "pops": pops}


def pop_doc(
    name: str = "Test POP",
    *,
    ipv4: list[str] | None = None,
    ipv6: list[str] | None = None,
    port_range: tuple[int, int] = (27015, 27060),
    geo: list[float] | None = None,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    relays: list[dict[str, Any]] = []
    for address in ipv4 or []:
        relays.append({"ipv4": address, "port_range": list(port_range)})
    for address in ipv6 or []:
        relays.append({"ipv6": address, "port_range": list(port_range)})
    out: dict[str, Any] = {"desc": name, "relays": relays}
    if geo is not None:
        out["geo"] = geo
    if aliases:
        out["aliases"] = aliases
    return out


def snapshot(pops: dict[str, Any], *, revision: int = 1):
    return parse_valve(raw_doc(pops, revision=revision), appid=730, source="test")
