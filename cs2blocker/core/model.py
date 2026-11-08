"""Core data model for cs2blocker.

Everything here is deliberately immutable and free of I/O so the rule generator
and validators can be unit-tested without touching the network or the firewall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class Region(StrEnum):
    EUROPE = "Europe"
    NORTH_AMERICA = "North America"
    SOUTH_AMERICA = "South America"
    ASIA = "Asia"
    OCEANIA = "Oceania"
    AFRICA = "Africa"
    MIDDLE_EAST = "Middle East"
    UNKNOWN = "Unknown"


class Mode(StrEnum):
    BLOCK = "block"
    ONLY = "only"


class Protocol(StrEnum):
    UDP = "udp"
    ALL = "all"


class BackendName(StrEnum):
    AUTO = "auto"
    NFT = "nft"
    IPTABLES = "iptables"


@dataclass(frozen=True, slots=True)
class Relay:
    """A single SDR relay endpoint.

    ``port_end`` is inclusive, matching Valve's ``port_range`` field.
    """

    address: str
    port_start: int
    port_end: int

    @property
    def version(self) -> int:
        return 6 if ":" in self.address else 4

    @property
    def port_text(self) -> str:
        if self.port_start == self.port_end:
            return str(self.port_start)
        return f"{self.port_start}-{self.port_end}"


@dataclass(frozen=True, slots=True)
class Pop:
    code: str
    name: str
    region: Region
    relays: tuple[Relay, ...] = ()
    geo: tuple[float, float] | None = None
    tier: int | None = None
    aliases: tuple[str, ...] = ()

    @property
    def relay_count(self) -> int:
        return len(self.relays)

    @property
    def has_relays(self) -> bool:
        return bool(self.relays)

    @property
    def ipv4_relays(self) -> tuple[Relay, ...]:
        return tuple(r for r in self.relays if r.version == 4)

    @property
    def ipv6_relays(self) -> tuple[Relay, ...]:
        return tuple(r for r in self.relays if r.version == 6)

    @property
    def label(self) -> str:
        return f"{self.code} {self.name} ({self.region.value})"


@dataclass(frozen=True, slots=True)
class SdrSnapshot:
    appid: int
    revision: int
    fetched_at: str
    source: str
    pops: Mapping[str, Pop]

    def sorted_pops(self) -> tuple[Pop, ...]:
        return tuple(self.pops[c] for c in sorted(self.pops))

    def with_relays(self) -> tuple[Pop, ...]:
        return tuple(p for p in self.sorted_pops() if p.has_relays)

    def pop_codes(self) -> frozenset[str]:
        return frozenset(self.pops)

    def relay_addresses(self) -> frozenset[str]:
        return frozenset(r.address for p in self.pops.values() for r in p.relays)


@dataclass(frozen=True, slots=True)
class Selection:
    mode: Mode = Mode.BLOCK
    pops: frozenset[str] = field(default_factory=frozenset)
    block_forward: bool = False
    protocol: Protocol = Protocol.UDP

    def normalized(self) -> "Selection":
        return Selection(
            mode=Mode(self.mode),
            pops=frozenset(self.pops),
            block_forward=bool(self.block_forward),
            protocol=Protocol(self.protocol),
        )


@dataclass(frozen=True, slots=True)
class Preset:
    """A named selection shortcut.

    ``regions`` are resolved against the current snapshot when the preset is
    applied; ``pops`` are explicit codes.  Applying a preset stores the
    resulting explicit POP list in the selection, so a later relay refresh does
    not silently change an allow-only selection.
    """

    key: str
    name: str
    mode: Mode
    regions: tuple[Region, ...] = ()
    pops: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class Config:
    appid: int = 730
    selection: Selection = field(default_factory=Selection)
    presets: dict[str, Preset] = field(default_factory=dict)
    backend: BackendName = BackendName.AUTO
    notify_on_change: bool = True
    version: int = 1

    def normalized(self) -> "Config":
        return Config(
            appid=int(self.appid),
            selection=self.selection.normalized(),
            presets={k: p for k, p in self.presets.items()},
            backend=BackendName(self.backend),
            notify_on_change=bool(self.notify_on_change),
            version=int(self.version),
        )


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    changed: bool
    revision_changed: bool
    added_addresses: frozenset[str] = frozenset()
    removed_addresses: frozenset[str] = frozenset()
    added_pops: frozenset[str] = frozenset()
    removed_pops: frozenset[str] = frozenset()

    @property
    def summary(self) -> str:
        if not self.changed:
            return "relay list unchanged"
        parts = []
        if self.revision_changed:
            parts.append("revision changed")
        if self.added_addresses or self.removed_addresses:
            parts.append(
                f"+{len(self.added_addresses)}/-{len(self.removed_addresses)} relay addresses"
            )
        if self.added_pops or self.removed_pops:
            parts.append(f"+{len(self.added_pops)}/-{len(self.removed_pops)} POPs")
        return ", ".join(parts) or "relay list changed"


@dataclass(frozen=True, slots=True)
class FirewallPlan:
    """The validated, deterministic description of what should be installed."""

    mode: Mode
    protocol: Protocol
    block_forward: bool
    selected: frozenset[str]
    blocked: tuple[Pop, ...]
    allowed: tuple[str, ...]
    port_ranges: tuple[tuple[int, int], ...]
    warnings: tuple[str, ...] = ()
    revision: int = 0

    @property
    def blocked_codes(self) -> tuple[str, ...]:
        return tuple(p.code for p in self.blocked)

    @property
    def relay_count(self) -> int:
        return sum(p.relay_count for p in self.blocked)

    @property
    def has_rules(self) -> bool:
        if not self.blocked:
            return False
        if self.protocol is Protocol.UDP and not self.port_ranges:
            return False
        return True
