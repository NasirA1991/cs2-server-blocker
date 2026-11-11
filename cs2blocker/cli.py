"""``cs2blockctl`` — unprivileged front-end and sudo/pkexec elevator."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from . import __version__
from .core.config import default_config, load_config
from .core.diagnostics import as_json as diagnostics_json
from .core.diagnostics import collect as collect_diagnostics
from .core.diagnostics import summarize as summarize_checks
from .core.model import Mode, Region, SdrSnapshot
from .core.ping import ping_many, pop_ping_target, rtt_text
from .core.presets import load_default_presets
from .core.regions import REGION_ORDER, group_pops
from .core.selection import allow_codes, block_codes
from .core.sdr import load_bundled, load_snapshot_file
from .core.state import Paths, read_optional_json
from .core.util import (
    Cs2BlockerError,
    format_ranges,
    human_age,
    parse_pop_codes,
    which_fixed,
)

INSTALLED_HELPER = Path("/usr/lib/cs2blocker/cs2blocker-helper")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _paths() -> Paths:
    return Paths.default()


def _fail(message: str, code: int = 1) -> int:
    print(f"cs2blockctl: {message}", file=sys.stderr)
    return code


def _load_config(paths: Paths, *, required: bool = False):
    try:
        config = load_config(paths, require_root=False, missing_ok=True)
    except Cs2BlockerError as exc:
        if required:
            raise
        print(f"cs2blockctl: ignoring bad config: {exc}", file=sys.stderr)
        return default_config()
    if config is None:
        if required:
            return default_config()
        return default_config()
    return config


def _load_snapshot(paths: Paths, appid: int) -> tuple[SdrSnapshot, str]:
    cache = paths.cache_file(appid)
    if cache.exists():
        try:
            return load_snapshot_file(cache, appid=appid, require_root=False), "cache"
        except Cs2BlockerError as exc:
            print(f"cs2blockctl: ignoring bad cached relay list: {exc}", file=sys.stderr)
    return load_bundled(appid), "bundled"


def _requested_region(value: str | None) -> Region | None:
    if not value:
        return None
    normalized = value.strip().lower()
    for region in REGION_ORDER:
        if region.value.lower() == normalized or region.name.lower() == normalized:
            return region
    raise Cs2BlockerError(f"unknown region: {value!r}")


def _helper_argv() -> list[str]:
    if INSTALLED_HELPER.is_file():
        return [str(INSTALLED_HELPER)]
    dev = REPO_ROOT / "cs2blocker-helper"
    if dev.is_file():
        return [str(dev)]
    return [sys.executable, "-m", "cs2blocker.helper"]


def _installed_helper() -> bool:
    return INSTALLED_HELPER.is_file()


def _run_helper(helper_args: list[str], *, force_sudo: bool = False) -> int:
    argv = _helper_argv()
    if os.geteuid() == 0:
        command = [*argv, *helper_args]
        result = subprocess.run(command, check=False)
        return result.returncode
    if force_sudo:
        sudo = which_fixed("sudo")
        if not sudo:
            return _fail("sudo is not installed; run the helper as root")
        return subprocess.run([sudo, *argv, *helper_args], check=False).returncode
    if not _installed_helper():
        return _fail(
            "cs2blocker is not installed system-wide. Run:\n"
            "  sudo ./packaging/install.sh\n"
            "then retry, or use --sudo with a development checkout."
        )
    pkexec = which_fixed("pkexec")
    if not pkexec:
        return _fail(
            "pkexec is not installed. Re-run with --sudo, or as root:\n  "
            + shlex.join(["sudo", str(INSTALLED_HELPER), *helper_args])
        )
    command = [pkexec, str(INSTALLED_HELPER), *helper_args]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode in (126, 127):
        lowered = (result.stderr or "").lower()
        if "agent" in lowered or "not authorized" in lowered or "dismissed" in lowered:
            print(
                "cs2blockctl: polkit could not authenticate (no agent or cancelled).\n"
                "Run this instead:\n  "
                + shlex.join(["sudo", str(INSTALLED_HELPER), *helper_args]),
                file=sys.stderr,
            )
    return result.returncode


def _apply_selection(selection: Mode, pops: frozenset[str], force_sudo: bool) -> int:
    helper_args = ["apply", "--mode", selection.value, "--pops", ",".join(sorted(pops))]
    return _run_helper(helper_args, force_sudo=force_sudo)


def list_command(paths: Paths, args: argparse.Namespace) -> int:
    config = _load_config(paths)
    snapshot, origin = _load_snapshot(paths, config.appid)
    try:
        region = _requested_region(args.region)
    except Cs2BlockerError as exc:
        return _fail(str(exc))
    pops = [p for p in snapshot.sorted_pops() if region is None or p.region is region]
    if args.json:
        payload = {
            "appid": snapshot.appid,
            "revision": snapshot.revision,
            "fetched_at": snapshot.fetched_at,
            "origin": origin,
            "pops": [
                {
                    "code": p.code,
                    "name": p.name,
                    "region": p.region.value,
                    "relays": p.relay_count,
                    "aliases": list(p.aliases),
                    "geo": list(p.geo) if p.geo else None,
                    "port_ranges": [list(r) for r in _pop_ranges(p)],
                }
                for p in pops
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for grouped_region in REGION_ORDER:
        group = [p for p in pops if p.region is grouped_region]
        if not group:
            continue
        print(f"{grouped_region.value} ({len(group)} POP{'s' if len(group) != 1 else ''})")
        for pop in group:
            ports = format_ranges(_pop_ranges(pop)) if pop.relays else "no relays"
            print(f"  {pop.code:5} {pop.name[:44]:44} {pop.relay_count:2} relay(s)  {ports}")
    print(
        f"\ndata: revision {snapshot.revision}, {origin}, {human_age(snapshot.fetched_at)}"
    )
    return 0


def _pop_ranges(pop) -> tuple[tuple[int, int], ...]:
    from .core.util import merge_ranges

    return merge_ranges((r.port_start, r.port_end) for r in pop.relays)


def status_command(paths: Paths, args: argparse.Namespace) -> int:
    from . import helper

    return helper.status_command(paths, SimpleNamespace(json=args.json))


def counters_command(paths: Paths, args: argparse.Namespace) -> int:
    interval = max(0.5, float(args.interval))
    while True:
        document = read_optional_json(paths.status_file, require_root=False)
        if args.json:
            print(json.dumps(document or {}, indent=2, sort_keys=True))
        elif not isinstance(document, dict):
            print("no live counters yet (monitor service not running or rules inactive)")
        else:
            active = "active" if document.get("active") else "inactive"
            print(
                f"{active} · backend={document.get('backend', '?')} · "
                f"updated {human_age(document.get('updated_at'))}"
            )
            counters = document.get("counters", {})
            if not counters:
                print("no counters (the monitor may not be running)")
            else:
                print(f"{'POP':5} {'packets':>10} {'bytes':>14}")
                for code, values in sorted(counters.items()):
                    if isinstance(values, dict):
                        print(
                            f"{code:5} {int(values.get('packets', 0)):10d} "
                            f"{int(values.get('bytes', 0)):14d}"
                        )
        if not args.watch:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def ping_command(paths: Paths, args: argparse.Namespace) -> int:
    config = _load_config(paths)
    snapshot, _ = _load_snapshot(paths, config.appid)
    try:
        region = _requested_region(args.region)
    except Cs2BlockerError as exc:
        return _fail(str(exc))
    if args.pops:
        try:
            codes = parse_pop_codes(args.pops)
        except Cs2BlockerError as exc:
            return _fail(str(exc))
        unknown = sorted(codes - snapshot.pop_codes())
        if unknown:
            return _fail(f"unknown POP code(s): {', '.join(unknown)}")
        pops = [snapshot.pops[code] for code in sorted(codes)]
    else:
        pops = [p for p in snapshot.sorted_pops() if p.relays]
    if region is not None:
        pops = [p for p in pops if p.region is region]
    targets = {p.code: pop_ping_target(p.relays) for p in pops}
    addresses = [addr for addr in targets.values() if addr]
    results = ping_many(addresses, timeout=args.timeout)
    if args.json:
        print(
            json.dumps(
                {
                    code: {
                        "address": addr,
                        "rtt_ms": results[addr].rtt_ms if addr and addr in results else None,
                        "method": results[addr].method if addr and addr in results else None,
                    }
                    for code, addr in targets.items()
                    if addr
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"{'POP':5} {'location':44} {'rtt':>8}  address")
    for pop in pops:
        address = targets.get(pop.code)
        result = results.get(address) if address else None
        print(f"{pop.code:5} {pop.name[:44]:44} {rtt_text(result):>8}  {address or '—'}")
    return 0


def doctor_command(paths: Paths, args: argparse.Namespace) -> int:
    if args.privileged:
        helper_args = ["doctor"] + (["--json"] if args.json else [])
        return _run_helper(helper_args, force_sudo=args.sudo)
    checks = collect_diagnostics(paths, privileged=os.geteuid() == 0)
    if args.json:
        print(diagnostics_json(checks))
    else:
        for check in checks:
            mark = "OK  " if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
            print(f"[{mark}] {check.name}: {check.detail}")
        summary = summarize_checks(checks)
        print(
            f"\n{len(checks)} checks: {summary['error']} error(s), "
            f"{summary['warning']} warning(s), {summary['ok']} ok"
        )
        if os.geteuid() != 0:
            print("run `sudo cs2blockctl doctor` for kernel/backend checks")
    return 1 if any(not c.ok and c.severity == "error" for c in checks) else 0


def preset_command(paths: Paths, args: argparse.Namespace) -> int:
    config = _load_config(paths)
    if args.list:
        presets = config.presets or load_default_presets()
        if not presets:
            print("no presets")
        for key, preset in sorted(presets.items()):
            print(
                f"{key:20} {preset.name:30} mode={preset.mode.value:5} "
                f"{len(preset.pops)} explicit POP(s)"
                + (f", regions={','.join(r.value for r in preset.regions)}" if preset.regions else "")
            )
        return 0
    if not args.name:
        return _fail("preset requires a name (or --list)")
    if args.save:
        helper_args = ["preset-save", "--name", args.name]
        return _run_helper(helper_args, force_sudo=args.sudo)
    if args.delete:
        return _run_helper(["preset-delete", "--name", args.name], force_sudo=args.sudo)
    return _run_helper(["apply", "--preset", args.name], force_sudo=args.sudo)


def block_command(paths: Paths, args: argparse.Namespace) -> int:
    config = _load_config(paths)
    snapshot, _ = _load_snapshot(paths, config.appid)
    try:
        codes = parse_pop_codes(args.pops)
    except Cs2BlockerError as exc:
        return _fail(str(exc))
    unknown = sorted(codes - snapshot.pop_codes())
    if unknown:
        return _fail(f"unknown POP code(s): {', '.join(unknown)}")
    updated = block_codes(config.selection, codes)
    return _apply_selection(updated.mode, updated.pops, args.sudo)


def unblock_command(paths: Paths, args: argparse.Namespace) -> int:
    config = _load_config(paths)
    snapshot, _ = _load_snapshot(paths, config.appid)
    try:
        codes = parse_pop_codes(args.pops)
    except Cs2BlockerError as exc:
        return _fail(str(exc))
    unknown = sorted(codes - snapshot.pop_codes())
    if unknown:
        return _fail(f"unknown POP code(s): {', '.join(unknown)}")
    updated = allow_codes(config.selection, codes)
    return _apply_selection(updated.mode, updated.pops, args.sudo)


def only_command(paths: Paths, args: argparse.Namespace) -> int:
    config = _load_config(paths)
    snapshot, _ = _load_snapshot(paths, config.appid)
    try:
        codes = parse_pop_codes(args.pops)
    except Cs2BlockerError as exc:
        return _fail(str(exc))
    unknown = sorted(codes - snapshot.pop_codes())
    if unknown:
        return _fail(f"unknown POP code(s): {', '.join(unknown)}")
    if not codes and not args.force:
        return _fail(
            "refusing an empty allow-only selection (it blocks every POP); pass --force if you mean it"
        )
    return _apply_selection(Mode.ONLY, codes, args.sudo)


def apply_command(paths: Paths, args: argparse.Namespace) -> int:
    return _run_helper(["apply"], force_sudo=args.sudo)


def refresh_command(paths: Paths, args: argparse.Namespace) -> int:
    return _run_helper(["refresh"], force_sudo=args.sudo)


def clear_command(paths: Paths, args: argparse.Namespace) -> int:
    return _run_helper(["clear"], force_sudo=args.sudo)


def pause_command(paths: Paths, args: argparse.Namespace) -> int:
    return _run_helper(["pause"], force_sudo=args.sudo)


def resume_command(paths: Paths, args: argparse.Namespace) -> int:
    return _run_helper(["resume"], force_sudo=args.sudo)


def persistence_command(paths: Paths, args: argparse.Namespace) -> int:
    return _run_helper(["persistence", args.action], force_sudo=args.sudo)


def version_command(paths: Paths, args: argparse.Namespace) -> int:
    print(f"cs2blockctl {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cs2blockctl",
        description="Control CS2 SDR datacenter blocking rules",
    )
    parser.add_argument(
        "--sudo",
        action="store_true",
        help="use sudo for privileged operations instead of pkexec",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list POPs")
    listing.add_argument("--region")
    listing.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="show rules/cache/persistence status")
    status.add_argument("--json", action="store_true")

    block = sub.add_parser("block", help="make POPs blocked (mode-aware)")
    block.add_argument("pops", nargs="+")

    unblock = sub.add_parser("unblock", help="make POPs allowed (mode-aware)")
    unblock.add_argument("pops", nargs="+")

    only = sub.add_parser("only", help="allow only the listed POPs")
    only.add_argument("pops", nargs="*")
    only.add_argument("--force", action="store_true", help="allow an empty selection")

    preset = sub.add_parser("preset", help="apply or manage presets")
    preset.add_argument("name", nargs="?")
    preset.add_argument("--list", action="store_true")
    preset.add_argument("--save", action="store_true")
    preset.add_argument("--delete", action="store_true")

    refresh = sub.add_parser("refresh", help="fetch a fresh relay list and re-apply")
    apply_p = sub.add_parser("apply", help="re-apply the saved selection")
    clear = sub.add_parser("clear", help="remove all rules and empty the selection")
    sub.add_parser("pause", help="remove rules but keep the saved selection")
    sub.add_parser("resume", help="re-apply the saved selection after pause")

    ping = sub.add_parser("ping", help="ICMP ping POP relays")
    ping.add_argument("pops", nargs="*")
    ping.add_argument("--region")
    ping.add_argument("--timeout", type=float, default=1.0)
    ping.add_argument("--json", action="store_true")

    counters = sub.add_parser("counters", help="show live drop counters")
    counters.add_argument("--json", action="store_true")
    counters.add_argument("--watch", action="store_true")
    counters.add_argument("--interval", type=float, default=2.0)

    doctor = sub.add_parser("doctor", help="run diagnostic checks")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--privileged", action="store_true", help="run the root checks via pkexec/sudo")

    persistence = sub.add_parser("persistence", help="enable or disable boot persistence")
    persistence.add_argument("action", choices=["enable", "disable"])

    sub.add_parser("version", help="print version")
    return parser


HANDLERS = {
    "list": list_command,
    "status": status_command,
    "block": block_command,
    "unblock": unblock_command,
    "only": only_command,
    "preset": preset_command,
    "refresh": refresh_command,
    "apply": apply_command,
    "clear": clear_command,
    "pause": pause_command,
    "resume": resume_command,
    "ping": ping_command,
    "counters": counters_command,
    "doctor": doctor_command,
    "persistence": persistence_command,
    "version": version_command,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = Paths.default()
    handler = HANDLERS[args.command]
    try:
        return handler(paths, args)
    except Cs2BlockerError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
