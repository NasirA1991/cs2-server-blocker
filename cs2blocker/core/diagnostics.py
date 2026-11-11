"""Read-only system checks used by ``doctor`` and the GUI status banner."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import load_config
from .model import BackendName
from .sdr import load_bundled, load_snapshot_file
from .state import Paths, read_json, read_optional_json
from .util import BackendError, age_seconds, human_age, run, which_fixed

UNITS = (
    "cs2blocker.service",
    "cs2blocker-refresh.timer",
    "cs2blocker-monitor.service",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "info"
    privileged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "severity": self.severity,
            "privileged": self.privileged,
        }


def _systemctl(*args: str) -> tuple[int, str]:
    binary = which_fixed("systemctl")
    if not binary:
        return 127, "systemctl not found"
    result = run([binary, *args], timeout=10.0)
    return result.returncode, (result.stdout or result.stderr).strip()


def _unit_state(unit: str) -> tuple[str, str]:
    enabled_code, enabled = _systemctl("is-enabled", unit)
    active_code, active = _systemctl("is-active", unit)
    enabled_text = enabled if enabled_code in (0, 1) else "unknown"
    active_text = active if active_code in (0, 3) else "unknown"
    return enabled_text, active_text


def _file_check(path: Path, *, require_root: bool = False) -> Check:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return Check(path.name, False, f"missing: {path}", "warning")
    if not stat.S_ISREG(st.st_mode):
        return Check(path.name, False, f"not a regular file: {path}", "error")
    if require_root and st.st_uid != 0:
        return Check(path.name, False, f"not root-owned: {path}", "error")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return Check(path.name, False, f"group/world-writable: {path}", "error")
    return Check(path.name, True, f"{path} mode {oct(stat.S_IMODE(st.st_mode))}")


def _installed_code_check() -> Check:
    """Refuse to trust a helper whose own code is user-writable.

    This mirrors the file-ownership checks used for config/state.  The
    installer must leave /usr/lib/cs2blocker root-owned; a source checkout or
    an old ``cp -a`` install can otherwise turn Apply into root code execution.
    """
    root = Path("/usr/lib/cs2blocker")
    if not root.exists():
        return Check("installed code", True, "not installed system-wide (development tree)", "info")
    bad: list[str] = []
    try:
        for current, dirs, files in os.walk(root):
            for name in (*dirs, *files):
                path = Path(current) / name
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode):
                    bad.append(f"{path}: symlink in installed code")
                elif st.st_uid != 0:
                    bad.append(f"{path}: owned by uid {st.st_uid}")
                elif st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    bad.append(f"{path}: group/world-writable")
                if len(bad) >= 5:
                    break
            if len(bad) >= 5:
                break
    except OSError as exc:
        return Check("installed code", False, f"cannot inspect {root}: {exc}", "error")
    if bad:
        return Check(
            "installed code",
            False,
            "root helper code is writable by a non-root user: " + "; ".join(bad),
            "error",
        )
    return Check("installed code", True, f"{root} is root-owned and not group/world-writable")


def collect(paths: Paths, *, privileged: bool = False) -> list[Check]:
    checks: list[Check] = []
    checks.append(_installed_code_check())
    nft = which_fixed("nft")
    if not nft:
        checks.append(Check("nft binary", False, "nft is not installed", "error"))
    else:
        result = run([nft, "--version"], timeout=10.0)
        checks.append(
            Check(
                "nft binary",
                result.returncode == 0,
                (result.stdout or result.stderr).strip() or "unknown version",
                "info" if result.returncode == 0 else "error",
            )
        )
        if privileged:
            probe = run([nft, "list", "tables"], timeout=10.0)
            detail = "kernel accepts nft netlink commands"
            if probe.returncode != 0:
                detail = probe.stderr.strip() or probe.stdout.strip() or "nft list tables failed"
            checks.append(
                Check(
                    "nft kernel support",
                    probe.returncode == 0,
                    detail,
                    "error" if probe.returncode != 0 else "info",
                    privileged=True,
                )
            )
        else:
            checks.append(
                Check(
                    "nft kernel support",
                    True,
                    "not checked without root (run sudo cs2blockctl doctor)",
                    "info",
                    privileged=True,
                )
            )

    have_ipset = which_fixed("ipset")
    checks.append(
        Check(
            "ipset fallback",
            bool(have_ipset),
            "ipset installed" if have_ipset else "ipset is not installed (only needed without nft)",
            "warning" if not have_ipset else "info",
        )
    )
    have_iptables = which_fixed("iptables")
    checks.append(
        Check(
            "iptables fallback",
            bool(have_iptables),
            "iptables installed" if have_iptables else "iptables is not installed",
            "info" if have_iptables else "warning",
        )
    )

    config = None
    try:
        config = load_config(paths, require_root=privileged, missing_ok=True)
    except Exception as exc:
        checks.append(Check("config", False, f"cannot read config: {exc}", "error", privileged))
    checks.append(_file_check(paths.config_file, require_root=privileged))
    checks.append(_file_check(paths.cache_file(730), require_root=privileged))
    checks.append(_file_check(paths.state_file, require_root=privileged))

    snapshot = None
    try:
        if paths.cache_file(730).is_file():
            snapshot = load_snapshot_file(paths.cache_file(730), require_root=privileged)
        else:
            snapshot = load_bundled()
        seconds = age_seconds(snapshot.fetched_at)
        stale = seconds is None or seconds > 8 * 86400 or snapshot.source == "bundled"
        checks.append(
            Check(
                "SDR cache",
                not stale,
                f"revision {snapshot.revision}, source {snapshot.source}, {human_age(snapshot.fetched_at)}"
                + (" (stale or bundled)" if stale else ""),
                "warning" if stale else "info",
            )
        )
    except Exception as exc:
        checks.append(Check("SDR cache", False, f"cannot load SDR data: {exc}", "error", privileged))

    state = None
    try:
        state = read_optional_json(paths.state_file, require_root=False)
    except Exception as exc:
        checks.append(Check("state", False, f"cannot read state: {exc}", "warning"))
    live = None
    try:
        live = read_optional_json(paths.status_file, require_root=False)
    except Exception:
        pass
    active = bool((live or {}).get("active", (state or {}).get("active", False)))
    checks.append(
        Check(
            "firewall table",
            active,
            "cs2blocker rules are active" if active else "cs2blocker rules are not active",
            "info" if active else "warning",
        )
    )

    for unit in UNITS:
        enabled, active_text = _unit_state(unit)
        is_active = active_text == "active"
        if unit.endswith(".timer"):
            ok = enabled == "enabled"
            detail = f"enabled={enabled} active={active_text}"
        elif unit.endswith("monitor.service"):
            ok = True
            detail = f"active={active_text}"
        else:
            ok = enabled == "enabled"
            detail = f"enabled={enabled} active={active_text}"
        checks.append(Check(unit, ok, detail, "info" if ok else "warning"))

    pkexec = which_fixed("pkexec")
    checks.append(
        Check(
            "polkit pkexec",
            bool(pkexec),
            "pkexec installed" if pkexec else "pkexec is not installed",
            "info" if pkexec else "error",
        )
    )
    polkitd_code, polkitd = _systemctl("is-active", "polkit")
    checks.append(
        Check(
            "polkit daemon",
            polkitd_code == 0,
            f"polkit: {polkitd or 'inactive'}",
            "info" if polkitd_code == 0 else "warning",
        )
    )
    agent = None
    pgrep = which_fixed("pgrep")
    if pgrep:
        for candidate in (
            "polkit-kde-authentication-agent-1",
            "polkit-gnome-authentication-agent-1",
            "lxpolkit",
            "polkit-mate-authentication-agent-1",
        ):
            result = run([pgrep, "-f", candidate], timeout=5.0)
            if result.returncode == 0:
                agent = candidate
                break
    checks.append(
        Check(
            "polkit agent",
            bool(agent),
            f"agent running: {agent}" if agent else "no polkit agent found; use sudo cs2blockctl",
            "info" if agent else "warning",
        )
    )

    for service in ("ufw", "firewalld", "docker"):
        code, out = _systemctl("is-active", service)
        checks.append(
            Check(
                f"{service} service",
                True,
                f"active={out}" if code == 3 or code in (0, 3) else f"state={out}",
                "info" if code not in (0,) else "warning",
            )
        )

    nftables_conf = Path("/etc/nftables.conf")
    try:
        text = nftables_conf.read_text(encoding="utf-8", errors="replace")
        has_flush = "flush ruleset" in text
        checks.append(
            Check(
                "nftables.conf",
                not has_flush,
                "contains 'flush ruleset'; our table may be removed if this file is loaded manually"
                if has_flush
                else "no 'flush ruleset' found",
                "warning" if has_flush else "info",
            )
        )
    except OSError:
        checks.append(Check("nftables.conf", True, "not present", "info"))

    if config is not None and snapshot is not None:
        try:
            from .ruleset import build_plan

            plan = build_plan(snapshot, config.selection)
            for warning in plan.warnings:
                checks.append(Check("selection warning", False, warning, "warning"))
        except Exception as exc:
            checks.append(Check("selection", False, str(exc), "error"))

    if privileged:
        try:
            from .backends import make_backend

            backend = make_backend(config.backend if config else BackendName.AUTO)
            status = backend.status()
            checks.append(
                Check(
                    "backend status",
                    status.active,
                    f"{status.backend}: active={status.active}, blocked={len(status.blocked)}",
                    "info" if status.active else "warning",
                    privileged=True,
                )
            )
        except BackendError as exc:
            checks.append(Check("backend status", False, str(exc), "error", privileged=True))
        except Exception as exc:
            checks.append(Check("backend status", False, str(exc), "error", privileged=True))
    return checks


def summarize(checks: list[Check]) -> dict[str, int]:
    counts = {"ok": 0, "warning": 0, "error": 0}
    for check in checks:
        if check.severity == "error" and not check.ok:
            counts["error"] += 1
        elif check.severity == "warning" and not check.ok:
            counts["warning"] += 1
        elif check.ok:
            counts["ok"] += 1
    return counts


def as_json(checks: list[Check]) -> str:
    return json.dumps(
        {"checks": [c.to_dict() for c in checks], "summary": summarize(checks)},
        indent=2,
        sort_keys=True,
    )
