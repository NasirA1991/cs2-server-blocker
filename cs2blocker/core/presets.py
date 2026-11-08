"""Built-in and user presets, stored in config.json (seeded from TOML)."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .model import Mode, Pop, Preset, Region, SdrSnapshot
from .util import ConfigError, ValidationError, parse_pop_codes, sanitize_text, slugify, validate_preset_key


def _preset_from_mapping(key: str, raw: Mapping[str, Any]) -> Preset:
    try:
        mode = Mode(str(raw.get("mode", "block")))
    except ValueError as exc:
        raise ConfigError(f"preset {key}: invalid mode") from exc
    name = sanitize_text(raw.get("name") or key, max_len=64) or key
    regions: list[Region] = []
    for item in raw.get("regions", []) or []:
        try:
            region = Region(str(item))
        except ValueError as exc:
            raise ConfigError(f"preset {key}: unknown region {item!r}") from exc
        if region not in regions:
            regions.append(region)
    pops = parse_pop_codes(raw.get("pops", []) or [])
    return Preset(key=validate_preset_key(key), name=name, mode=mode, regions=tuple(regions), pops=pops)


def load_default_presets(path: Path | None = None) -> dict[str, Preset]:
    from .state import Paths

    source = path or Paths.bundled_presets()
    if not source.is_file():
        return {}
    try:
        with source.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read default presets {source}: {exc}") from exc
    raw_presets = document.get("presets", {})
    if not isinstance(raw_presets, Mapping):
        raise ConfigError("default presets file has no [presets] table")
    return {
        key: _preset_from_mapping(key, raw)
        for key, raw in raw_presets.items()
        if isinstance(raw, Mapping)
    }


def preset_to_dict(preset: Preset) -> dict[str, Any]:
    return {
        "name": preset.name,
        "mode": preset.mode.value,
        "regions": [r.value for r in preset.regions],
        "pops": sorted(preset.pops),
    }


def preset_from_dict(key: str, raw: Mapping[str, Any]) -> Preset:
    return _preset_from_mapping(key, raw)


def resolve_preset(preset: Preset, snapshot: SdrSnapshot) -> tuple[frozenset[str], tuple[str, ...]]:
    warnings: list[str] = []
    codes: set[str] = set(preset.pops)
    for region in preset.regions:
        codes.update(code for code, pop in snapshot.pops.items() if pop.region is region)
    unknown = frozenset(codes - snapshot.pop_codes())
    if unknown:
        warnings.append(
            f"preset {preset.key} references unknown POP(s): {', '.join(sorted(unknown))}"
        )
    return frozenset(codes & snapshot.pop_codes()), tuple(warnings)


def preset_for_selection(
    name: str,
    *,
    mode: Mode,
    pops: frozenset[str],
    snapshot: SdrSnapshot,
) -> Preset:
    key = slugify(name)
    if not key:
        raise ValidationError("preset name is empty")
    known = frozenset(pops & snapshot.pop_codes())
    unknown = pops - known
    if unknown:
        raise ValidationError(f"unknown POP(s): {', '.join(sorted(unknown))}")
    return Preset(key=validate_preset_key(key), name=sanitize_text(name, max_len=64), mode=mode, pops=known)


def find_preset(presets: Mapping[str, Preset], key: str) -> Preset:
    normalized = key.strip().lower()
    if normalized in presets:
        return presets[normalized]
    for preset in presets.values():
        if preset.name.lower() == normalized:
            return preset
    raise ConfigError(f"unknown preset: {key}")


def grouped_pops(snapshot: SdrSnapshot) -> dict[Region, tuple[Pop, ...]]:
    from .regions import group_pops

    grouped = group_pops(snapshot.sorted_pops())
    return {region: tuple(items) for region, items in grouped.items()}
