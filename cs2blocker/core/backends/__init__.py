"""Backend selection: nftables first, iptables/ipset only as a fallback."""

from __future__ import annotations

from ..model import BackendName
from ..util import BackendError
from .base import Backend
from .iptables import IptablesBackend
from .nft import NftBackend


def make_backend(name: BackendName | str = BackendName.AUTO, *, runner=None) -> Backend:
    try:
        requested = BackendName(str(name))
    except ValueError as exc:
        raise BackendError(f"unknown backend: {name!r}") from exc
    nft = NftBackend(runner)
    iptables = IptablesBackend(runner)
    if requested in (BackendName.AUTO, BackendName.NFT):
        if nft.available(deep=True):
            return nft
        if requested is BackendName.NFT:
            raise BackendError(
                "nft backend requested but nft is missing or the kernel has no nf_tables support"
            )
    if requested in (BackendName.AUTO, BackendName.IPTABLES):
        if iptables.available(deep=True):
            return iptables
        if requested is BackendName.IPTABLES:
            raise BackendError(
                "iptables backend requested but iptables/ipset are missing"
            )
    raise BackendError(
        "no usable firewall backend: install nftables, or iptables plus ipset"
    )


def inspect_backends(*, runner=None) -> dict[str, dict[str, object]]:
    nft = NftBackend(runner)
    iptables = IptablesBackend(runner)
    return {
        "nft": {
            "installed": bool(nft._nft_path),  # noqa: SLF001 - diagnostics
            "version": nft.version(),
            "deep_ok": nft.available(deep=True),
            "destroy": nft.supports_destroy() if nft.available() else False,
        },
        "iptables": {
            "installed": iptables.available(),
            "version": iptables.version(),
            "deep_ok": iptables.available(deep=True),
        },
    }
