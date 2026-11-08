"""Backend interface shared by nftables and iptables/ipset."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol

from ..model import FirewallPlan


class Runner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        input_text: str | None = None,
        timeout: float = 60.0,
    ) -> "CommandResult": ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True, slots=True)
class Counter:
    packets: int
    bytes: int


@dataclass(slots=True)
class BackendStatus:
    backend: str
    active: bool
    blocked: tuple[str, ...] = ()
    counters: dict[str, Counter] = field(default_factory=dict)
    table: str = "inet cs2blocker"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    backend: str
    active: bool
    blocked: tuple[str, ...]
    relay_count: int
    message: str
    counters: dict[str, Counter] = field(default_factory=dict)


class Backend(ABC):
    name: str

    @abstractmethod
    def available(self, *, deep: bool = False) -> bool:
        raise NotImplementedError

    @abstractmethod
    def version(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def apply(self, plan: FirewallPlan) -> ApplyResult:
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> ApplyResult:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> BackendStatus:
        raise NotImplementedError

    @abstractmethod
    def reset_counters(self) -> None:
        raise NotImplementedError
