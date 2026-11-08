"""Curated POP-to-region mapping.

Valve's SDR document does not contain a region field (only a description and a
``[longitude, latitude]`` pair), and at least one POP has coordinates that do
not match its description.  Regions are therefore curated by stable POP code.
Unknown future codes map to ``Region.UNKNOWN`` and are never guessed from
coordinates.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from .model import Pop, Region

REGION_ORDER: tuple[Region, ...] = (
    Region.EUROPE,
    Region.NORTH_AMERICA,
    Region.SOUTH_AMERICA,
    Region.ASIA,
    Region.OCEANIA,
    Region.AFRICA,
    Region.MIDDLE_EAST,
    Region.UNKNOWN,
)

REGION_BY_POP: dict[str, Region] = {
    # Europe
    "ams": Region.EUROPE,
    "dfra": Region.EUROPE,
    "dlhr": Region.EUROPE,
    "dsto": Region.EUROPE,
    "dvie": Region.EUROPE,
    "fsn": Region.EUROPE,
    "fra": Region.EUROPE,
    "hel": Region.EUROPE,
    "lhr": Region.EUROPE,
    "mad": Region.EUROPE,
    "par": Region.EUROPE,
    "sto": Region.EUROPE,
    "sto2": Region.EUROPE,
    "vie": Region.EUROPE,
    "waw": Region.EUROPE,
    # North America
    "atl": Region.NORTH_AMERICA,
    "dfw": Region.NORTH_AMERICA,
    "eat": Region.NORTH_AMERICA,
    "iad": Region.NORTH_AMERICA,
    "lax": Region.NORTH_AMERICA,
    "ord": Region.NORTH_AMERICA,
    "sea": Region.NORTH_AMERICA,
    # Asia
    "bom2": Region.ASIA,
    "maa2": Region.ASIA,
    "ctu": Region.ASIA,
    "ctum": Region.ASIA,
    "ctut": Region.ASIA,
    "ctuu": Region.ASIA,
    "pek": Region.ASIA,
    "pekm": Region.ASIA,
    "pekt": Region.ASIA,
    "peku": Region.ASIA,
    "pvg": Region.ASIA,
    "pvgm": Region.ASIA,
    "pvgt": Region.ASIA,
    "pvgu": Region.ASIA,
    "tgd": Region.ASIA,
    "tgdm": Region.ASIA,
    "tgdt": Region.ASIA,
    "tgdu": Region.ASIA,
    "hkg": Region.ASIA,
    "sgp": Region.ASIA,
    "seo": Region.ASIA,
    "tyo": Region.ASIA,
    # South America
    "eze": Region.SOUTH_AMERICA,
    "gru": Region.SOUTH_AMERICA,
    "lim": Region.SOUTH_AMERICA,
    "scl": Region.SOUTH_AMERICA,
    # Oceania
    "gum": Region.OCEANIA,
    "syd": Region.OCEANIA,
    # Africa
    "jnb": Region.AFRICA,
    # Middle East
    "dfjr": Region.MIDDLE_EAST,
    "dxb": Region.MIDDLE_EAST,
}


def region_for(code: str) -> Region:
    return REGION_BY_POP.get(code, Region.UNKNOWN)


def group_pops(pops: Iterable[Pop]) -> dict[Region, list[Pop]]:
    grouped: dict[Region, list[Pop]] = defaultdict(list)
    for pop in pops:
        grouped[pop.region].append(pop)
    for items in grouped.values():
        items.sort(key=lambda p: (p.code, p.name))
    return dict(grouped)


def ordered_regions(pops: Mapping[str, Pop] | Iterable[Pop]) -> tuple[Region, ...]:
    values = pops.values() if isinstance(pops, Mapping) else pops
    present = {p.region for p in values}
    return tuple(r for r in REGION_ORDER if r in present)


def format_region(region: Region) -> str:
    return region.value
