"""nftables backend: one dedicated table, one atomic transaction per apply."""

from __future__ import annotations

import json
import re
from typing import Any

from ..model import FirewallPlan
from ..ruleset import TABLE_NAME, render_clear, render_nft
from ..util import BackendError, run, utc_now, which_fixed
from .base import ApplyResult, Backend, BackendStatus, CommandResult, Counter

_VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


class NftBackend(Backend):
    name = "nft"

    def __init__(self, runner=None, *, nft_path: str | None = None) -> None:
        self._runner = runner
        self._nft_path = nft_path or which_fixed("nft")
        self._version: tuple[int, int, int] | None = None

    def _run(self, argv: list[str], *, input_text: str | None = None, timeout: float = 60.0) -> CommandResult:
        if self._runner is not None:
            return self._runner(argv, input_text=input_text, timeout=timeout)
        completed = run(argv, input_text=input_text, timeout=timeout)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def _nft(self) -> str:
        if not self._nft_path:
            raise BackendError("nft is not installed")
        return self._nft_path

    def available(self, *, deep: bool = False) -> bool:
        if not self._nft_path:
            return False
        result = self._run([self._nft(), "--version"], timeout=10.0)
        if not result.ok:
            return False
        if deep:
            probe = self._run([self._nft(), "list", "tables"], timeout=10.0)
            return probe.ok
        return True

    def version(self) -> str:
        if not self._nft_path:
            return "not installed"
        result = self._run([self._nft(), "--version"], timeout=10.0)
        if not result.ok:
            return "unknown"
        return result.stdout.strip() or result.stderr.strip() or "unknown"

    def version_tuple(self) -> tuple[int, int, int]:
        if self._version is not None:
            return self._version
        match = _VERSION_RE.search(self.version())
        if not match:
            self._version = (0, 0, 0)
        else:
            self._version = tuple(int(part) for part in match.groups())  # type: ignore[assignment]
        return self._version

    def supports_destroy(self) -> bool:
        return self.version_tuple() >= (1, 0, 7)

    def table_exists(self) -> bool:
        result = self._run([self._nft(), "list", "table", "inet", TABLE_NAME], timeout=15.0)
        if result.ok:
            return True
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "no such file or directory" in text or "does not exist" in text:
            return False
        raise BackendError(
            f"cannot inspect nft table inet {TABLE_NAME}: {result.stderr.strip() or result.stdout.strip()}"
        )

    def _replace_strategy(self) -> str:
        if self.supports_destroy():
            return "destroy"
        return "delete" if self.table_exists() else "none"

    def apply(self, plan: FirewallPlan) -> ApplyResult:
        if not plan.has_rules:
            return self.clear()
        strategy = self._replace_strategy()
        text = render_nft(
            plan,
            revision=plan.revision,
            replace_strategy=strategy,
            version="0.1.1",
            generated_at=utc_now(),
        )
        check = self._run([self._nft(), "-c", "-f", "-"], input_text=text, timeout=30.0)
        if not check.ok:
            raise BackendError(
                "nft rejected the generated ruleset (check mode):\n"
                + (check.stderr.strip() or check.stdout.strip() or "unknown error")
            )
        apply_result = self._run([self._nft(), "-f", "-"], input_text=text, timeout=30.0)
        if not apply_result.ok:
            raise BackendError(
                "nft failed to apply the ruleset; the previous table is unchanged:\n"
                + (apply_result.stderr.strip() or apply_result.stdout.strip() or "unknown error")
            )
        return ApplyResult(
            backend=self.name,
            active=True,
            blocked=plan.blocked_codes,
            relay_count=plan.relay_count,
            message=(
                f"applied {len(plan.blocked)} POP(s), {plan.relay_count} relay endpoint(s), "
                f"{'UDP' if plan.protocol.value == 'udp' else 'all protocols'}"
            ),
        )

    def clear(self) -> ApplyResult:
        strategy = "destroy" if self.supports_destroy() else self._replace_strategy()
        if strategy == "none":
            return ApplyResult(
                backend=self.name,
                active=False,
                blocked=(),
                relay_count=0,
                message="no nft table to remove",
            )
        text = render_clear(replace_strategy=strategy, version="0.1.1", generated_at=utc_now())
        result = self._run([self._nft(), "-f", "-"], input_text=text, timeout=30.0)
        if not result.ok:
            raise BackendError(
                "nft failed to remove the table:\n"
                + (result.stderr.strip() or result.stdout.strip() or "unknown error")
            )
        return ApplyResult(
            backend=self.name,
            active=False,
            blocked=(),
            relay_count=0,
            message=f"removed table inet {TABLE_NAME}",
        )

    def _table_json(self) -> dict[str, Any] | None:
        result = self._run(
            [self._nft(), "-j", "list", "table", "inet", TABLE_NAME], timeout=15.0
        )
        if not result.ok:
            text = f"{result.stdout}\n{result.stderr}".lower()
            if "no such file or directory" in text or "does not exist" in text:
                return None
            raise BackendError(
                f"cannot read nft status: {result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"nft returned invalid JSON: {exc}") from exc

    def status(self) -> BackendStatus:
        document = self._table_json()
        if document is None:
            return BackendStatus(backend=self.name, active=False)
        counters: dict[str, Counter] = {}
        blocked: set[str] = set()
        active = False
        for item in document.get("nftables", []):
            if not isinstance(item, dict):
                continue
            if "table" in item:
                active = True
            counter = item.get("counter")
            if isinstance(counter, dict):
                name = str(counter.get("name", ""))
                if name.startswith("cnt_"):
                    counters[name[4:]] = Counter(
                        packets=int(counter.get("packets", 0)),
                        bytes=int(counter.get("bytes", 0)),
                    )
            set_obj = item.get("set")
            if isinstance(set_obj, dict):
                name = str(set_obj.get("name", ""))
                if name.startswith("pop_") and not name.endswith("_v6"):
                    blocked.add(name[4:])
        return BackendStatus(
            backend=self.name,
            active=active,
            blocked=tuple(sorted(blocked)),
            counters=counters,
        )

    def reset_counters(self) -> None:
        if not self.table_exists():
            return
        result = self._run(
            [self._nft(), "reset", "counters", "table", "inet", TABLE_NAME], timeout=15.0
        )
        if not result.ok:
            raise BackendError(
                "cannot reset nft counters: "
                + (result.stderr.strip() or result.stdout.strip() or "unknown error")
            )
