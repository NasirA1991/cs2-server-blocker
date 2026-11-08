"""Human-friendly city/country labels for Valve's POP descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.model import Pop, Region

_VENDORS = (
    "Alibaba Cloud ",
    "Datapacket ",
    "Tencent ",
    "Hetzner ",
    "Steam ",
)
_NETWORKS = {"Mobile", "Telecom", "Unicom"}
_US_STATES = {
    "Georgia",
    "Washington",
    "Texas",
    "California",
    "Illinois",
    "Virginia",
    "Oregon",
    "New York",
}
_COUNTRY_ALIASES = {
    "England": "United Kingdom",
    "UAE": "United Arab Emirates",
    "USA": "United States",
    "South Korea": "South Korea",
}


@dataclass(frozen=True, slots=True)
class Location:
    title: str
    subtitle: str
    city: str
    country: str
    region: Region
    code: str


def _split_country(description: str) -> tuple[str, str]:
    match = re.match(r"^(.*?)(?:\s*\(([^()]*)\))?\s*$", description.strip())
    if not match:
        return description.strip(), ""
    return match.group(1).strip(), (match.group(2) or "").strip()


def _strip_vendor(text: str) -> tuple[str, str]:
    for vendor in _VENDORS:
        if text.startswith(vendor):
            return text[len(vendor) :].strip(), vendor.strip()
    return text.strip(), ""


def _country(raw: str, base: str) -> tuple[str, str]:
    if not raw:
        if base.casefold() == "guam":
            return "Guam", ""
        return "", ""
    if raw in _US_STATES:
        return "United States", raw
    return _COUNTRY_ALIASES.get(raw, raw), ""


def parse_location(pop: Pop) -> Location:
    base, raw_country = _split_country(pop.name)
    country, state = _country(raw_country, base)
    area = ""
    network = ""
    vendor = ""

    if " - " in base:
        left, right = base.split(" - ", 1)
        left, vendor = _strip_vendor(left.strip())
        if right in _NETWORKS:
            city = left
            network = right
        else:
            city = right.strip()
            area = left
    else:
        city, vendor = _strip_vendor(base)
        if not city:
            city = base

    title = f"{city}, {country}" if country and country != city else city
    if area:
        subtitle = f"{area}, {country}" if country else area
    elif network:
        subtitle = f"{vendor or 'Partner'} {network}" if vendor else network
    elif vendor:
        subtitle = vendor
    else:
        subtitle = pop.region.value
    if state:
        subtitle = f"{state} · {pop.region.value}"
    elif pop.region.value not in subtitle:
        subtitle = f"{subtitle} · {pop.region.value}" if subtitle else pop.region.value

    return Location(
        title=title,
        subtitle=subtitle,
        city=city,
        country=country,
        region=pop.region,
        code=pop.code,
    )


def display_title(pop: Pop) -> str:
    return parse_location(pop).title
