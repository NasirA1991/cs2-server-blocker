"""iptables + ipset fallback, used only when nftables is unavailable.

iptables has no user-defined tables, so this backend uses a dedicated chain and
a single jump from OUTPUT/FORWARD.  It never flushes another chain and never
touches nat/mangle.  Updates are best-effort atomic: a new chain is built and
the jump is swapped, with the old chain left in place if anything fails.
"""

from __future__ import annotations

import re
from ..model import FirewallPlan, Protocol
from ..util import BackendError, run, which_fixed
from .base import ApplyResult, Backend, BackendStatus, CommandResult, Counter

CHAIN = "CS2BLOCKER"
NEW_CHAIN = "CS2BLOCKER_NEW"
SET_V4_PREFIX = "cs2b_"
SET_V6_PREFIX = "cs2b6_"
_COUNTER_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+\S+.*?/\*\s*cs2blocker:([a-z0-9]+)\s*\*/")


class IptablesBackend(Backend):
    name = "iptables"

    def __init__(self, runner=None) -> None:
        self._runner = runner
        self._iptables = which_fixed("iptables")
        self._ip6tables = which_fixed("ip6tables")
        self._ipset = which_fixed("ipset")

    def _run(self, argv: list[str], *, input_text: str | None = None, timeout: float = 60.0) -> CommandResult:
        if self._runner is not None:
            return self._runner(argv, input_text=input_text, timeout=timeout)
        completed = run(argv, input_text=input_text, timeout=timeout)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def available(self, *, deep: bool = False) -> bool:
        if not (self._iptables and self._ipset):
            return False
        if deep:
            probe = self._run([self._iptables, "-w", "5", "-S", "OUTPUT"], timeout=10.0)
            return probe.ok
        return True

    def version(self) -> str:
        if not self._iptables:
            return "not installed"
        result = self._run([self._iptables, "--version"], timeout=10.0)
        return (result.stdout or result.stderr).strip() or "unknown"

    def _ipt(self, *args: str) -> list[str]:
        if not self._iptables:
            raise BackendError("iptables is not installed")
        return [self._iptables, "-w", "5", *args]

    def _ip6(self, *args: str) -> list[str]:
        if not self._ip6tables:
            raise BackendError("ip6tables is not installed")
        return [self._ip6tables, "-w", "5", *args]

    def _set(self, *args: str) -> list[str]:
        if not self._ipset:
            raise BackendError("ipset is not installed")
        return [self._ipset, *args]

    def _require_ok(self, argv: list[str], what: str) -> None:
        result = self._run(argv, timeout=30.0)
        if not result.ok:
            raise BackendError(
                f"{what} failed: {result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
            )

    def _chain_exists(self, ipt: str, chain: str) -> bool:
        result = self._run([ipt, "-w", "5", "-S", chain], timeout=10.0)
        return result.ok

    def _jump_present(self, ipt: str, parent: str, chain: str) -> bool:
        return self._run([ipt, "-w", "5", "-C", parent, "-j", chain], timeout=10.0).ok

    def _delete_jump(self, ipt: str, parent: str, chain: str) -> None:
        # Remove every matching jump; normally there is exactly one.
        for _ in range(8):
            if not self._jump_present(ipt, parent, chain):
                return
            self._run([ipt, "-w", "5", "-D", parent, "-j", chain], timeout=10.0)

    def _port_match_args(self, plan: FirewallPlan) -> list[str]:
        if plan.protocol is not Protocol.UDP or not plan.port_ranges:
            return []
        return [
            "-p",
            "udp",
            "-m",
            "multiport",
            "--dports",
            ",".join(f"{start}:{end}" if start != end else str(start) for start, end in plan.port_ranges),
        ]

    def _rule_args(self, plan: FirewallPlan, code: str, set_name: str) -> list[str]:
        return [
            *self._port_match_args(plan),
            "-m",
            "set",
            "--match-set",
            set_name,
            "dst",
            "-m",
            "comment",
            "--comment",
            f"cs2blocker:{code}",
            "-j",
            "DROP",
        ]

    def _swap_set(self, temp: str, final: str) -> None:
        # Both sets must exist.  swap is atomic from the kernel's perspective.
        self._require_ok(self._set("swap", temp, final), f"ipset swap {temp} {final}")
        self._require_ok(self._set("destroy", temp), f"ipset destroy {temp}")

    def apply(self, plan: FirewallPlan) -> ApplyResult:
        if not plan.has_rules:
            return self.clear()
        if not self._iptables or not self._ipset:
            raise BackendError(
                "iptables fallback requires iptables and ipset; install the 'ipset' package"
            )
        if plan.blocked and any(p.ipv6_relays for p in plan.blocked) and not self._ip6tables:
            raise BackendError("IPv6 relay data requires ip6tables")
        try:
            # 1. Prepare final sets atomically: fill a temp set, then swap it in.
            for pop in plan.blocked:
                v4 = [r.address for r in pop.ipv4_relays]
                v6 = [r.address for r in pop.ipv6_relays]
                if v4:
                    final = SET_V4_PREFIX + pop.code
                    temp = final + "_tmp"
                    self._ensure_final_set(final, "inet")
                    self._prepare_temp_set(temp, "inet", v4)
                    self._swap_set(temp, final)
                if v6:
                    final = SET_V6_PREFIX + pop.code
                    temp = final + "_tmp"
                    self._ensure_final_set(final, "inet6")
                    self._prepare_temp_set(temp, "inet6", v6)
                    self._swap_set(temp, final)

            # 2. Build the replacement chain referencing the final set names.
            for ipt, family in ((self._iptables, "inet"), (self._ip6tables, "inet6")):
                if not ipt:
                    continue
                self._delete_chain(ipt, NEW_CHAIN)
                self._require_ok([ipt, "-w", "5", "-N", NEW_CHAIN], f"create chain {NEW_CHAIN}")
                for pop in plan.blocked:
                    if family == "inet" and pop.ipv4_relays:
                        self._require_ok(
                            [ipt, "-w", "5", "-A", NEW_CHAIN, *self._rule_args(plan, pop.code, SET_V4_PREFIX + pop.code)],
                            f"add rule for {pop.code}",
                        )
                    if family == "inet6" and pop.ipv6_relays:
                        self._require_ok(
                            [ipt, "-w", "5", "-A", NEW_CHAIN, *self._rule_args(plan, pop.code, SET_V6_PREFIX + pop.code)],
                            f"add IPv6 rule for {pop.code}",
                        )

            # 3. Swap the jump(s), then delete the old chain and rename.
            self._swap_parent_chain(self._iptables, "OUTPUT")
            if plan.block_forward:
                self._swap_parent_chain(self._iptables, "FORWARD")
            else:
                self._delete_jump(self._iptables, "FORWARD", CHAIN)
                self._delete_jump(self._iptables, "FORWARD", NEW_CHAIN)
            if self._ip6tables:
                self._swap_parent_chain(self._ip6tables, "OUTPUT")
                if plan.block_forward:
                    self._swap_parent_chain(self._ip6tables, "FORWARD")

            # 4. Remove stale sets from a previous, larger selection.
            self._destroy_stale_sets({SET_V4_PREFIX + p.code for p in plan.blocked}, SET_V4_PREFIX)
            self._destroy_stale_sets({SET_V6_PREFIX + p.code for p in plan.blocked}, SET_V6_PREFIX)
        except BackendError:
            raise
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            raise BackendError(f"iptables fallback failed: {exc}") from exc
        return ApplyResult(
            backend=self.name,
            active=True,
            blocked=plan.blocked_codes,
            relay_count=plan.relay_count,
            message=f"applied {len(plan.blocked)} POP(s) via iptables/ipset",
        )

    def _ensure_final_set(self, name: str, family: str) -> None:
        self._require_ok(
            self._set("create", name, "hash:net", "family", family, "-exist"),
            f"ipset create {name}",
        )

    def _prepare_temp_set(self, name: str, family: str, addresses: list[str]) -> None:
        self._require_ok(
            self._set("create", name, "hash:net", "family", family, "-exist"),
            f"ipset create {name}",
        )
        self._require_ok(self._set("flush", name), f"ipset flush {name}")
        for address in addresses:
            self._require_ok(self._set("add", name, address, "-exist"), f"ipset add {name}")

    def _delete_chain(self, ipt: str, chain: str) -> None:
        if not self._chain_exists(ipt, chain):
            return
        self._run([ipt, "-w", "5", "-F", chain], timeout=10.0)
        self._run([ipt, "-w", "5", "-X", chain], timeout=10.0)

    def _swap_parent_chain(self, ipt: str, parent: str) -> None:
        self._delete_jump(ipt, parent, NEW_CHAIN)
        if not self._chain_exists(ipt, CHAIN):
            self._require_ok(
                [ipt, "-w", "5", "-I", parent, "1", "-j", NEW_CHAIN],
                f"insert jump in {parent}",
            )
            self._require_ok(
                [ipt, "-w", "5", "-E", NEW_CHAIN, CHAIN],
                f"rename {NEW_CHAIN}",
            )
            return
        self._require_ok(
            [ipt, "-w", "5", "-I", parent, "1", "-j", NEW_CHAIN],
            f"insert jump in {parent}",
        )
        self._delete_jump(ipt, parent, CHAIN)
        if self._chain_exists(ipt, CHAIN):
            self._run([ipt, "-w", "5", "-F", CHAIN], timeout=10.0)
            self._require_ok(
                [ipt, "-w", "5", "-X", CHAIN], f"delete old chain {CHAIN}"
            )
        self._require_ok(
            [ipt, "-w", "5", "-E", NEW_CHAIN, CHAIN],
            f"rename {NEW_CHAIN}",
        )

    def _destroy_stale_sets(self, keep: set[str], prefix: str) -> None:
        if not self._ipset:
            return
        result = self._run(self._set("list", "-n"), timeout=15.0)
        if not result.ok:
            return
        for name in result.stdout.split():
            if name.startswith(prefix) and name not in keep and not name.endswith("_tmp"):
                self._run(self._set("destroy", name), timeout=10.0)

    def clear(self) -> ApplyResult:
        if not self._iptables:
            return ApplyResult(self.name, False, (), 0, "iptables is not installed")
        for ipt in (self._iptables, self._ip6tables):
            if not ipt:
                continue
            for parent in ("OUTPUT", "FORWARD"):
                self._delete_jump(ipt, parent, CHAIN)
                self._delete_jump(ipt, parent, NEW_CHAIN)
            for chain in (CHAIN, NEW_CHAIN):
                self._delete_chain(ipt, chain)
        if self._ipset:
            result = self._run(self._set("list", "-n"), timeout=15.0)
            if result.ok:
                for name in result.stdout.split():
                    if name.startswith((SET_V4_PREFIX, SET_V6_PREFIX)):
                        self._run(self._set("destroy", name), timeout=10.0)
        return ApplyResult(self.name, False, (), 0, "removed iptables chain and sets")

    def _list_counters(self, ipt: str) -> dict[str, Counter]:
        result = self._run([ipt, "-w", "5", "-L", CHAIN, "-v", "-n", "-x"], timeout=15.0)
        if not result.ok:
            return {}
        counters: dict[str, Counter] = {}
        for line in result.stdout.splitlines():
            match = _COUNTER_RE.match(line)
            if not match:
                continue
            packets, nbytes, code = match.groups()
            previous = counters.get(code)
            if previous is None:
                counters[code] = Counter(int(packets), int(nbytes))
            else:
                counters[code] = Counter(
                    previous.packets + int(packets), previous.bytes + int(nbytes)
                )
        return counters

    def status(self) -> BackendStatus:
        if not self._iptables:
            return BackendStatus(backend=self.name, active=False)
        if not self._chain_exists(self._iptables, CHAIN):
            return BackendStatus(backend=self.name, active=False)
        counters = self._list_counters(self._iptables)
        if self._ip6tables:
            for code, counter in self._list_counters(self._ip6tables).items():
                previous = counters.get(code)
                if previous is None:
                    counters[code] = counter
                else:
                    counters[code] = Counter(
                        previous.packets + counter.packets,
                        previous.bytes + counter.bytes,
                    )
        return BackendStatus(
            backend=self.name,
            active=True,
            blocked=tuple(sorted(counters)),
            counters=counters,
        )

    def reset_counters(self) -> None:
        for ipt in (self._iptables, self._ip6tables):
            if ipt and self._chain_exists(ipt, CHAIN):
                self._run([ipt, "-w", "5", "-Z", CHAIN], timeout=10.0)
