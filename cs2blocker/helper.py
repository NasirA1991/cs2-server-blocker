#!/usr/bin/python3
"""Privileged helper for cs2blocker.

This is the only program that writes firewall rules, the root-owned SDR cache,
the root-owned config, and the root-owned state file.  It accepts POP codes,
mode/protocol flags and preset names only; never an IP address, path or nft
snippet.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .core.backends import make_backend
from .core.config import (
    Config,
    config_to_dict,
    default_config,
    load_config,
    save_config,
)
from .core.diagnostics import as_json as diagnostics_json
from .core.diagnostics import collect as collect_diagnostics
from .core.model import BackendName, Config as ConfigType, Mode, Protocol, Selection, SdrSnapshot
from .core.presets import find_preset, preset_for_selection, resolve_preset
from .core.ruleset import build_plan
from .core.sdr import (
    SdrError,
    diff_snapshots,
    fetch_raw,
    load_bundled,
    load_snapshot_file,
    parse_raw,
    save_snapshot_file,
)
from .core.state import Paths, lock, read_json, read_optional_json, remove_file, write_json
from .core.util import (
    BackendError,
    ConfigError,
    Cs2BlockerError,
    ValidationError,
    age_seconds,
    human_age,
    parse_pop_codes,
    run,
    slugify,
    utc_now,
    validate_preset_key,
    which_fixed,
)

STALE_AFTER_SECONDS = 8 * 86400
LOCK_TIMEOUT = 30.0
UNIT_SERVICE = "cs2blocker.service"
UNIT_REFRESH_TIMER = "cs2blocker-refresh.timer"
UNIT_MONITOR = "cs2blocker-monitor.service"


class HelperError(Cs2BlockerError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


INSTALLED_CODE_ROOT = Path("/usr/lib/cs2blocker")


def _verify_installed_code() -> None:
    """Refuse to execute a system helper whose code a user could rewrite.

    The generic installer used to preserve a checkout's owner; that made
    ``helper.py`` writable by the invoking user even though root imports it.
    Packages are normally root-owned, but this check also protects tarball and
    hand-copied installs.
    """
    # Do not resolve symlinks before deciding whether this is installed code:
    # a symlinked helper.py must still be inspected at its installed path so a
    # user-writable target cannot bypass the checks by resolving outside /usr.
    code_dir = Path(__file__).absolute().parent
    if code_dir != INSTALLED_CODE_ROOT and INSTALLED_CODE_ROOT not in code_dir.parents:
        return
    paths = sorted(code_dir.rglob("*.py"))
    if not paths:
        raise HelperError(f"refusing to run: no installed helper code under {code_dir}")
    for path in paths:
        try:
            lst = path.lstat()
            st = path.stat()
        except OSError as exc:
            raise HelperError(f"cannot inspect installed helper code {path}: {exc}") from exc
        if stat.S_ISLNK(lst.st_mode):
            raise HelperError(f"refusing to run: installed helper code is a symlink: {path}")
        if st.st_uid != 0:
            raise HelperError(
                f"refusing to run: installed helper code is not root-owned: {path} "
                "(reinstall with the current packaging/install.sh)"
            )
        if st.st_mode & 0o022:
            raise HelperError(
                f"refusing to run: installed helper code is group/world-writable: {path}"
            )


def _require_root() -> None:
    if os.geteuid() != 0:
        raise HelperError(
            "the helper must run as root; use cs2blockctl with pkexec/sudo, "
            "or run: sudo /usr/lib/cs2blocker/cs2blocker-helper ...",
            code=1,
        )
    _verify_installed_code()


def _read_state(paths: Paths, *, require_root: bool = True) -> dict[str, Any]:
    document = read_optional_json(paths.state_file, require_root=require_root)
    return document if isinstance(document, dict) else {}


def _update_state(paths: Paths, **fields: Any) -> dict[str, Any]:
    state = _read_state(paths)
    state.update(fields)
    state["schema"] = 1
    state["updated_at"] = utc_now()
    write_json(paths.state_file, state, mode=0o644)
    return state


def _cache_info(snapshot: SdrSnapshot, origin: str, warnings: tuple[str, ...] = ()) -> dict[str, Any]:
    seconds = age_seconds(snapshot.fetched_at)
    stale = origin == "bundled" or seconds is None or seconds > STALE_AFTER_SECONDS
    return {
        "revision": snapshot.revision,
        "fetched_at": snapshot.fetched_at,
        "source": snapshot.source,
        "origin": origin,
        "age_seconds": seconds,
        "age_human": human_age(snapshot.fetched_at),
        "stale": stale,
        "warnings": list(warnings),
    }


def load_snapshot_for_use(paths: Paths, appid: int = 730) -> tuple[SdrSnapshot, str, tuple[str, ...]]:
    warnings: list[str] = []
    cache_path = paths.cache_file(appid)
    try:
        if cache_path.exists():
            snapshot = load_snapshot_file(cache_path, appid=appid, require_root=True)
            return snapshot, "cache", ()
    except Cs2BlockerError as exc:
        warnings.append(f"ignoring invalid cache: {exc}")
    try:
        snapshot = load_bundled(appid)
    except (Cs2BlockerError, OSError, json.JSONDecodeError) as exc:
        raise HelperError(f"no valid SDR data available: {exc}", code=2) from exc
    return snapshot, "bundled", tuple(warnings)


def _selection_from_args(
    config: ConfigType, snapshot: SdrSnapshot, args: argparse.Namespace
) -> tuple[Selection, tuple[str, ...]]:
    preset_name = getattr(args, "preset", None)
    pops_arg = getattr(args, "pops", None)
    mode_arg = getattr(args, "mode", None)
    protocol_arg = getattr(args, "protocol", None)
    forward_arg = getattr(args, "forward", None)
    if preset_name and pops_arg is not None:
        raise HelperError("use either --preset or --pops, not both")
    if preset_name:
        preset = find_preset(config.presets, preset_name)
        if mode_arg and Mode(mode_arg) is not preset.mode:
            raise HelperError("--mode conflicts with the selected preset")
        pops, warnings = resolve_preset(preset, snapshot)
        return (
            Selection(
                mode=preset.mode,
                pops=pops,
                block_forward=(
                    forward_arg if forward_arg is not None else config.selection.block_forward
                ),
                protocol=Protocol(protocol_arg) if protocol_arg else config.selection.protocol,
            ),
            warnings,
        )
    if pops_arg is not None:
        pops = parse_pop_codes(pops_arg)
        unknown = sorted(pops - snapshot.pop_codes())
        if unknown:
            raise HelperError(f"unknown POP code(s): {', '.join(unknown)}")
        return (
            Selection(
                mode=Mode(mode_arg) if mode_arg else config.selection.mode,
                pops=pops,
                block_forward=(
                    forward_arg if forward_arg is not None else config.selection.block_forward
                ),
                protocol=Protocol(protocol_arg) if protocol_arg else config.selection.protocol,
            ),
            (),
        )
    return (
        Selection(
            mode=Mode(mode_arg) if mode_arg else config.selection.mode,
            pops=config.selection.pops,
            block_forward=(
                forward_arg if forward_arg is not None else config.selection.block_forward
            ),
            protocol=Protocol(protocol_arg) if protocol_arg else config.selection.protocol,
        ),
        (),
    )


def _systemctl(*args: str, timeout: float = 10.0):
    binary = which_fixed("systemctl")
    if not binary:
        return None
    return run([binary, *args], timeout=timeout)


def start_monitor() -> None:
    result = _systemctl("start", "--no-block", UNIT_MONITOR)
    if result is not None and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        # A non-systemd environment (netns, container, Artix) is not an error;
        # the GUI simply falls back to the last state file.
        if detail and "systemd" not in detail.lower() and "bus" not in detail.lower():
            print(
                f"cs2blocker-helper: warning: could not start {UNIT_MONITOR}: {detail}",
                file=sys.stderr,
            )


def stop_monitor() -> None:
    _systemctl("stop", "--no-block", UNIT_MONITOR)


def _safe_active(paths: Paths) -> bool:
    return bool(_read_state(paths).get("active", False))


def _is_paused(paths: Paths) -> bool:
    return bool(_read_state(paths).get("paused", False))


def _apply_plan_and_state(
    paths: Paths,
    config: ConfigType,
    snapshot: SdrSnapshot,
    *,
    origin: str,
    backend: Any = None,
    paused: bool = False,
) -> tuple[Any, Any]:
    plan = build_plan(snapshot, config.selection)
    chosen = backend or make_backend(config.backend)
    if plan.has_rules:
        result = chosen.apply(plan)
    else:
        result = chosen.clear()
    _update_state(
        paths,
        appid=config.appid,
        backend=result.backend,
        active=result.active,
        table="inet cs2blocker",
        mode=config.selection.mode.value,
        selected=sorted(config.selection.pops),
        blocked=list(result.blocked),
        allowed=list(plan.allowed),
        relay_count=plan.relay_count,
        warnings=list(plan.warnings),
        message=result.message,
        error=None,
        ok=True,
        paused=paused,
        paused_at=None,
        last_apply_at=utc_now(),
        cache=_cache_info(snapshot, origin),
    )
    if result.active:
        start_monitor()
    else:
        stop_monitor()
    return plan, result


def _restore_config(paths: Paths, old_document: dict[str, Any] | None, existed: bool) -> None:
    if existed and old_document is not None:
        write_json(paths.config_file, old_document, mode=0o644)
    elif not existed:
        remove_file(paths.config_file)


def apply_command(paths: Paths, args: argparse.Namespace, *, backend: Any = None) -> int:
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        if getattr(args, "if_not_paused", False) and _is_paused(paths):
            print("paused: skipping boot-time apply; selection kept")
            return 0
        had_config = paths.config_file.exists()
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            config = default_config()
        old_document = config_to_dict(config) if had_config else None
        snapshot, origin, cache_warnings = load_snapshot_for_use(paths, config.appid)
        selection, selection_warnings = _selection_from_args(config, snapshot, args)
        new_config = Config(
            appid=config.appid,
            selection=selection,
            presets=dict(config.presets),
            backend=config.backend,
            notify_on_change=config.notify_on_change,
            version=config.version,
        )
        new_document = config_to_dict(new_config)
        changed = (not had_config) or new_document != old_document
        if changed:
            save_config(paths, new_config)
        try:
            plan, result = _apply_plan_and_state(
                paths, new_config, snapshot, origin=origin, backend=backend
            )
        except Exception as exc:
            if changed:
                try:
                    _restore_config(paths, old_document, had_config)
                except Exception as restore_exc:
                    print(
                        f"cs2blocker-helper: warning: could not restore config: {restore_exc}",
                        file=sys.stderr,
                    )
            _update_state(
                paths,
                ok=False,
                error=str(exc),
                active=_safe_active(paths),
                last_apply_at=utc_now(),
                cache=_cache_info(snapshot, origin, cache_warnings),
            )
            raise HelperError(f"apply failed: {exc}", code=2) from exc

        print(
            f"applied: {len(plan.blocked)} POP(s), {plan.relay_count} relay endpoint(s), "
            f"mode={new_config.selection.mode.value}, backend={result.backend}"
        )
        if plan.port_ranges:
            from .core.util import format_ranges

            print(f"udp ports: {format_ranges(plan.port_ranges)}")
        for warning in (*cache_warnings, *selection_warnings, *plan.warnings):
            print(f"warning: {warning}", file=sys.stderr)
        return 0


def _apply_saved_config(
    paths: Paths, config: ConfigType, snapshot: SdrSnapshot, *, origin: str
) -> tuple[Any, Any]:
    return _apply_plan_and_state(paths, config, snapshot, origin=origin)


def refresh_command(paths: Paths, args: argparse.Namespace) -> int:
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            config = default_config()
        paused = _is_paused(paths)
        old_snapshot: SdrSnapshot | None = None
        try:
            old_snapshot, old_origin, old_warnings = load_snapshot_for_use(paths, config.appid)
        except HelperError as exc:
            old_origin, old_warnings = "bundled", (str(exc),)
        try:
            raw = fetch_raw(config.appid)
            new_snapshot = parse_raw(raw, appid=config.appid, source="network")
        except (Cs2BlockerError, OSError) as exc:
            _update_state(
                paths,
                ok=False,
                refresh_error=str(exc),
                last_refresh_at=utc_now(),
                cache=_cache_info(old_snapshot, old_origin, old_warnings) if old_snapshot else {},
            )
            print(f"refresh failed: {exc}", file=sys.stderr)
            if old_snapshot is not None and not args.no_apply and not paused:
                try:
                    _apply_saved_config(paths, config, old_snapshot, origin=old_origin)
                except Exception as apply_exc:
                    print(f"re-apply of cached data failed: {apply_exc}", file=sys.stderr)
            if args.timer:
                return 0
            raise HelperError(f"refresh failed: {exc}", code=3) from exc
        save_snapshot_file(paths.cache_file(config.appid), new_snapshot)
        diff = diff_snapshots(old_snapshot, new_snapshot)
        if not args.no_apply and not paused:
            try:
                _apply_saved_config(paths, config, new_snapshot, origin="cache")
            except Exception as exc:
                _update_state(
                    paths,
                    ok=False,
                    refresh_error=None,
                    last_refresh_at=utc_now(),
                    relay_list_changed=diff.changed,
                    relay_list_summary=diff.summary,
                    cache=_cache_info(new_snapshot, "cache"),
                    error=f"apply after refresh failed: {exc}",
                )
                raise HelperError(f"refresh succeeded but apply failed: {exc}", code=2) from exc
        _update_state(
            paths,
            ok=True,
            refresh_error=None,
            last_refresh_at=utc_now(),
            relay_list_changed=diff.changed,
            relay_list_summary=diff.summary,
            cache=_cache_info(new_snapshot, "cache"),
            **({"paused": True, "active": False, "blocked": [], "relay_count": 0} if paused else {}),
        )
        print(
            f"refreshed appid {config.appid}: revision {new_snapshot.revision}, "
            f"{len(new_snapshot.pops)} POPs, {sum(p.relay_count for p in new_snapshot.pops.values())} relays"
        )
        if diff.changed:
            print(f"relay list changed: {diff.summary}")
        return 0


def clear_command(paths: Paths, args: argparse.Namespace | None = None) -> int:
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            config = default_config()
        try:
            backend = make_backend(config.backend)
            result = backend.clear()
        except BackendError as exc:
            if not (which_fixed("nft") or which_fixed("iptables")):
                result = None
            else:
                _update_state(paths, ok=False, error=str(exc), active=_safe_active(paths))
                raise HelperError(f"clear failed: {exc}", code=2) from exc
        config.selection = Selection()
        save_config(paths, config)
        stop_monitor()
        _update_state(
            paths,
            ok=True,
            active=False,
            paused=False,
            paused_at=None,
            backend=result.backend if result else "none",
            blocked=[],
            selected=[],
            relay_count=0,
            message=result.message if result else "no firewall backend present",
            error=None,
        )
        print(result.message if result else "no firewall backend present; nothing to clean")
        return 0


def pause_command(paths: Paths, args: argparse.Namespace | None = None) -> int:
    """Remove firewall rules without dropping the saved selection.

    ``clear`` means "forget everything"; ``pause`` is the temporary SDR
    switch-off.  The selection, mode and presets stay in config so ``resume``
    can restore exactly the same rules.
    """
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            config = default_config()
        try:
            backend = make_backend(config.backend)
            result = backend.clear()
        except BackendError as exc:
            if not (which_fixed("nft") or which_fixed("iptables")):
                result = None
            else:
                _update_state(paths, ok=False, error=str(exc), active=_safe_active(paths))
                raise HelperError(f"pause failed: {exc}", code=2) from exc
        stop_monitor()
        _update_state(
            paths,
            ok=True,
            active=False,
            paused=True,
            paused_at=utc_now(),
            backend=result.backend if result else "none",
            blocked=[],
            selected=sorted(config.selection.pops),
            mode=config.selection.mode.value,
            relay_count=0,
            message=result.message if result else "no firewall backend present",
            error=None,
        )
        print("paused: rules removed; saved selection kept")
        print(f"selected: {', '.join(sorted(config.selection.pops)) or '-'}")
        return 0


def resume_command(
    paths: Paths,
    args: argparse.Namespace | None = None,
    *,
    backend: Any = None,
) -> int:
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        if not _is_paused(paths):
            print("not paused; nothing to resume")
            return 0
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            config = default_config()
        snapshot, origin, cache_warnings = load_snapshot_for_use(paths, config.appid)
        try:
            plan, result = _apply_plan_and_state(
                paths, config, snapshot, origin=origin, backend=backend, paused=False
            )
        except Exception as exc:
            _update_state(
                paths,
                ok=False,
                active=False,
                paused=True,
                error=f"resume failed: {exc}",
                last_apply_at=utc_now(),
                cache=_cache_info(snapshot, origin, cache_warnings),
            )
            raise HelperError(f"resume failed: {exc}", code=2) from exc
        _update_state(paths, last_resume_at=utc_now())
        print(
            f"resumed: {len(plan.blocked)} POP(s), {plan.relay_count} relay endpoint(s), "
            f"mode={config.selection.mode.value}, backend={result.backend}"
        )
        for warning in (*cache_warnings, *plan.warnings):
            print(f"warning: {warning}", file=sys.stderr)
        return 0


def _unit_state_text(result) -> str:
    if result is None:
        return "unavailable"
    text = (result.stdout or "").strip()
    if text:
        return text
    error = (result.stderr or "").strip()
    if "failed to connect" in error.lower() or "bus" in error.lower():
        return "unavailable"
    return "unknown"


def _unit_enabled(unit: str) -> str:
    result = _systemctl("is-enabled", unit)
    if result is None:
        return "unavailable"
    if result.returncode == 0:
        return (result.stdout or "").strip() or "enabled"
    if result.returncode == 1 and not (result.stderr or "").strip():
        return "disabled"
    if "disabled" in (result.stderr or "").lower():
        return "disabled"
    return "unavailable"


def _unit_active(unit: str) -> str:
    result = _systemctl("is-active", unit)
    return _unit_state_text(result)


def status_command(paths: Paths, args: argparse.Namespace) -> int:
    config = None
    config_error = None
    try:
        config = load_config(paths, require_root=False, missing_ok=True)
    except Exception as exc:
        config_error = str(exc)
    state = _read_state(paths, require_root=False)
    live = read_optional_json(paths.status_file, require_root=False)
    persistence = {
        "service": _unit_enabled(UNIT_SERVICE),
        "service_active": _unit_active(UNIT_SERVICE),
        "timer": _unit_enabled(UNIT_REFRESH_TIMER),
        "timer_active": _unit_active(UNIT_REFRESH_TIMER),
        "monitor_active": _unit_active(UNIT_MONITOR),
    }
    paused = bool(state.get("paused", False))
    active = bool(
        (live or {}).get("active", state.get("active", False))
        if isinstance(live, dict)
        else state.get("active", False)
    ) and not paused
    document = {
        "config": config_to_dict(config) if config else None,
        "config_error": config_error,
        "state": state,
        "live": live if isinstance(live, dict) else None,
        "persistence": persistence,
        "paused": paused,
        "active": active,
    }
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    print(f"active: {'yes' if document['active'] else 'no'}")
    print(f"paused: {'yes' if paused else 'no'}")
    print(f"backend: {state.get('backend', 'unknown')}")
    mode = state.get("mode", "unknown")
    print(f"mode: {mode}")
    print(f"selected: {', '.join(state.get('selected', [])) or '-'}")
    print(f"blocked: {', '.join(state.get('blocked', [])) or '-'}")
    cache = state.get("cache", {})
    if cache:
        print(
            f"relay list: revision {cache.get('revision', '?')}, "
            f"{cache.get('origin', '?')}, {cache.get('age_human', '?')}"
            + (" (STALE)" if cache.get("stale") else "")
        )
    last_refresh = state.get("last_refresh_at")
    if last_refresh:
        print(f"last refresh: {human_age(last_refresh)}")
    if state.get("refresh_error"):
        print(f"last refresh error: {state['refresh_error']}")
    if state.get("relay_list_changed"):
        print(f"relay list changed: {state.get('relay_list_summary', 'yes')}")
    for warning in state.get("warnings", []) or []:
        print(f"warning: {warning}")
    if state.get("error"):
        print(f"last apply error: {state['error']}")
    print(
        "persistence: "
        f"service={persistence['service']} ({persistence['service_active']}), "
        f"timer={persistence['timer']} ({persistence['timer_active']})"
    )
    if isinstance(live, dict) and live.get("counters"):
        counters = live["counters"]
        print("counters: " + ", ".join(f"{code}={value}" for code, value in sorted(counters.items())))
    if config_error:
        print(f"config error: {config_error}", file=sys.stderr)
    return 0


def persistence_command(paths: Paths, args: argparse.Namespace) -> int:
    _require_root()
    if not which_fixed("systemctl"):
        raise HelperError("systemctl is not available; enable the unit with your init system")
    units = [UNIT_SERVICE, UNIT_REFRESH_TIMER]
    action = "enable" if args.action == "enable" else "disable"
    result = _systemctl(action, *units)
    if result is None or not result.ok:
        detail = (result.stderr or result.stdout).strip() if result is not None else "systemctl missing"
        raise HelperError(f"systemctl {action} failed: {detail}", code=2)
    print(f"{action}d: {' '.join(units)}")
    return 0


def preset_save_command(paths: Paths, args: argparse.Namespace) -> int:
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            config = default_config()
        snapshot, _, _ = load_snapshot_for_use(paths, config.appid)
        selection, _ = _selection_from_args(config, snapshot, args)
        preset = preset_for_selection(
            args.name,
            mode=selection.mode,
            pops=selection.pops,
            snapshot=snapshot,
        )
        config.presets[preset.key] = preset
        save_config(paths, config)
        print(f"saved preset {preset.key!r} ({preset.name}), mode={preset.mode.value}, {len(preset.pops)} POP(s)")
        return 0


def preset_delete_command(paths: Paths, args: argparse.Namespace) -> int:
    _require_root()
    paths.ensure_dirs()
    with lock(paths, timeout=LOCK_TIMEOUT):
        config = load_config(paths, require_root=True, missing_ok=True)
        if config is None:
            raise HelperError("no config; no presets to delete", code=2)
        preset = find_preset(config.presets, args.name)
        del config.presets[preset.key]
        save_config(paths, config)
        print(f"deleted preset {preset.key!r}")
        return 0


def _monitor_payload(backend: Any) -> dict[str, Any]:
    try:
        status = backend.status()
        counters = {
            code: {"packets": counter.packets, "bytes": counter.bytes}
            for code, counter in status.counters.items()
        }
        return {
            "schema": 1,
            "updated_at": utc_now(),
            "active": status.active,
            "backend": status.backend,
            "table": "inet cs2blocker",
            "blocked": list(status.blocked),
            "counters": counters,
            "error": None,
        }
    except Exception as exc:
        return {
            "schema": 1,
            "updated_at": utc_now(),
            "active": False,
            "backend": getattr(backend, "name", "unknown"),
            "table": "inet cs2blocker",
            "blocked": [],
            "counters": {},
            "error": str(exc),
        }


def monitor_command(paths: Paths, args: argparse.Namespace) -> int:
    _require_root()
    # The monitor is sandboxed with ProtectSystem=strict and only /run is
    # writable.  Do not touch /etc or /var here; cs2blocker.service owns the
    # runtime directory and starts before this unit.
    try:
        paths.run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        if not paths.run_dir.is_dir():
            raise
    try:
        config = load_config(paths, require_root=False, missing_ok=True)
    except Cs2BlockerError:
        config = None
    backend = None
    backend_error = None
    try:
        backend = make_backend(config.backend if config else BackendName.AUTO)
    except Exception as exc:
        backend_error = str(exc)
    inactive = 0
    interval = max(0.5, float(args.interval))
    while True:
        if backend is None:
            payload = {
                "schema": 1,
                "updated_at": utc_now(),
                "active": False,
                "backend": "none",
                "table": "inet cs2blocker",
                "blocked": [],
                "counters": {},
                "error": backend_error,
            }
        else:
            payload = _monitor_payload(backend)
        write_json(paths.status_file, payload, mode=0o644)
        if args.once:
            return 0
        if not payload["active"]:
            inactive += 1
            if inactive >= 30:
                return 0
        else:
            inactive = 0
        time.sleep(interval)


def doctor_command(paths: Paths, args: argparse.Namespace) -> int:
    _require_root()
    checks = collect_diagnostics(paths, privileged=True)
    if args.json:
        print(diagnostics_json(checks))
    else:
        for check in checks:
            mark = "OK  " if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
            print(f"[{mark}] {check.name}: {check.detail}")
        errors = sum(1 for c in checks if not c.ok and c.severity == "error")
        warnings = sum(1 for c in checks if not c.ok and c.severity == "warning")
        print(f"\n{len(checks)} checks: {errors} error(s), {warnings} warning(s)")
    return 1 if any(not c.ok and c.severity == "error" for c in checks) else 0


def version_command(args: argparse.Namespace | None = None) -> int:
    print(f"cs2blocker-helper {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cs2blocker-helper",
        description="Privileged helper for CS2 Server Blocker",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser("apply", help="apply a selection")
    apply_parser.add_argument("--mode", choices=[m.value for m in Mode])
    apply_parser.add_argument("--pops", help="comma/space-separated POP codes")
    apply_parser.add_argument("--preset", help="apply a named preset")
    apply_parser.add_argument("--protocol", choices=[p.value for p in Protocol])
    apply_parser.add_argument("--forward", dest="forward", action="store_true", default=None)
    apply_parser.add_argument("--no-forward", dest="forward", action="store_false")
    apply_parser.add_argument(
        "--if-not-paused",
        action="store_true",
        help="no-op when blocking is paused (used by the boot service)",
    )

    refresh_parser = sub.add_parser("refresh", help="fetch a fresh SDR list and re-apply")
    refresh_parser.add_argument("--no-apply", action="store_true")
    refresh_parser.add_argument("--timer", action="store_true", help="exit 0 on transient network failure")

    sub.add_parser("clear", help="remove all rules and empty the saved selection")
    sub.add_parser("pause", help="remove rules but keep the saved selection")
    sub.add_parser("resume", help="re-apply the saved selection after pause")

    status_parser = sub.add_parser("status", help="show state/status")
    status_parser.add_argument("--json", action="store_true")

    persistence = sub.add_parser("persistence", help="enable/disable boot persistence")
    persistence.add_argument("action", choices=["enable", "disable"])

    preset_save = sub.add_parser("preset-save", help="save the current selection as a preset")
    preset_save.add_argument("--name", required=True)
    preset_save.add_argument("--mode", choices=[m.value for m in Mode])
    preset_save.add_argument("--pops")
    preset_save.add_argument("--protocol", choices=[p.value for p in Protocol])

    preset_delete = sub.add_parser("preset-delete", help="delete a preset")
    preset_delete.add_argument("--name", required=True)

    monitor = sub.add_parser("monitor", help="publish live counters for the GUI")
    monitor.add_argument("--interval", type=float, default=2.0)
    monitor.add_argument("--once", action="store_true")

    doctor = sub.add_parser("doctor", help="run diagnostic checks")
    doctor.add_argument("--json", action="store_true")

    sub.add_parser("version", help="print version")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = Paths.default()
    try:
        if args.command == "apply":
            return apply_command(paths, args)
        if args.command == "refresh":
            return refresh_command(paths, args)
        if args.command == "clear":
            return clear_command(paths, args)
        if args.command == "pause":
            return pause_command(paths, args)
        if args.command == "resume":
            return resume_command(paths, args)
        if args.command == "status":
            return status_command(paths, args)
        if args.command == "persistence":
            return persistence_command(paths, args)
        if args.command == "preset-save":
            return preset_save_command(paths, args)
        if args.command == "preset-delete":
            return preset_delete_command(paths, args)
        if args.command == "monitor":
            return monitor_command(paths, args)
        if args.command == "doctor":
            return doctor_command(paths, args)
        if args.command == "version":
            return version_command(args)
        parser.error(f"unknown command: {args.command}")
    except HelperError as exc:
        print(f"cs2blocker-helper: {exc}", file=sys.stderr)
        return exc.code
    except (Cs2BlockerError, ConfigError, ValidationError, BackendError, OSError) as exc:
        print(f"cs2blocker-helper: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cs2blocker-helper: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
