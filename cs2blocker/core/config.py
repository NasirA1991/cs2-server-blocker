"""Configuration schema and root-owned persistence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .model import BackendName, Config, Mode, Protocol, Selection
from .presets import load_default_presets, preset_from_dict, preset_to_dict
from .state import Paths, read_json, write_json
from .util import ConfigError, parse_pop_codes, sanitize_text

CONFIG_VERSION = 1


def default_config() -> Config:
    return Config(presets=load_default_presets(), selection=Selection())


def selection_to_dict(selection: Selection) -> dict[str, Any]:
    return {
        "mode": selection.mode.value,
        "pops": sorted(selection.pops),
        "block_forward": bool(selection.block_forward),
        "protocol": selection.protocol.value,
    }


def selection_from_dict(raw: Any) -> Selection:
    if not isinstance(raw, Mapping):
        raise ConfigError("selection must be an object")
    try:
        mode = Mode(str(raw.get("mode", "block")))
        protocol = Protocol(str(raw.get("protocol", "udp")))
    except ValueError as exc:
        raise ConfigError(f"invalid selection: {exc}") from exc
    pops_raw = raw.get("pops", [])
    if not isinstance(pops_raw, list):
        raise ConfigError("selection.pops must be a list")
    pops = parse_pop_codes([str(item) for item in pops_raw])
    return Selection(
        mode=mode,
        pops=pops,
        block_forward=bool(raw.get("block_forward", False)),
        protocol=protocol,
    )


def config_to_dict(config: Config) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "appid": int(config.appid),
        "backend": config.backend.value,
        "notify_on_change": bool(config.notify_on_change),
        "selection": selection_to_dict(config.selection),
        "presets": {
            key: preset_to_dict(preset) for key, preset in sorted(config.presets.items())
        },
    }


def config_from_dict(document: Any) -> Config:
    if not isinstance(document, Mapping):
        raise ConfigError("config must be a JSON object")
    version = document.get("version", 1)
    if version != CONFIG_VERSION:
        raise ConfigError(f"unsupported config version: {version!r}")
    try:
        backend = BackendName(str(document.get("backend", "auto")))
    except ValueError:
        backend = BackendName.AUTO
    appid = document.get("appid", 730)
    if isinstance(appid, bool) or not isinstance(appid, int):
        raise ConfigError("config.appid must be an integer")
    presets = {}
    raw_presets = document.get("presets", {})
    if isinstance(raw_presets, Mapping):
        for key, raw in raw_presets.items():
            if not isinstance(raw, Mapping):
                continue
            try:
                preset = preset_from_dict(str(key), raw)
            except (ConfigError, ValueError):
                # A broken optional preset must not prevent boot-time apply.
                continue
            presets[preset.key] = preset
    return Config(
        appid=appid,
        selection=selection_from_dict(document.get("selection", {})),
        presets=presets,
        backend=backend,
        notify_on_change=bool(document.get("notify_on_change", True)),
        version=CONFIG_VERSION,
    )


def load_config(
    paths: Paths,
    *,
    require_root: bool = True,
    missing_ok: bool = True,
) -> Config | None:
    document = read_json(paths.config_file, require_root=require_root, missing_ok=missing_ok)
    if document is None:
        return None
    return config_from_dict(document)


def load_config_or_default(paths: Paths, *, require_root: bool = True) -> Config:
    config = load_config(paths, require_root=require_root, missing_ok=True)
    return config if config is not None else default_config()


def save_config(paths: Paths, config: Config) -> None:
    write_json(paths.config_file, config_to_dict(config), mode=0o644)


def load_presets_from_file(path: Path) -> dict[str, Any]:
    """Small helper for CLI/GUI display; returns the raw user preset mapping."""
    document = read_json(path, require_root=False, missing_ok=True)
    if not isinstance(document, Mapping):
        return {}
    presets = document.get("presets", {})
    return presets if isinstance(presets, Mapping) else {}


def describe_selection(selection: Selection) -> str:
    if selection.mode is Mode.BLOCK:
        return f"block {len(selection.pops)} POP(s)"
    return f"allow only {len(selection.pops)} POP(s)"


def clean_preset_name(value: str) -> str:
    return sanitize_text(value, max_len=64)
