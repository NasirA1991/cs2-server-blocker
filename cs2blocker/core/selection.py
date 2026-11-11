"""Mode-aware selection transitions.

The CLI and GUI used to implement these rules independently.  Keeping them in
one place makes the verbs unambiguous:

* ``block_codes`` means "make these POPs blocked".
* ``allow_codes`` means "make these POPs allowed".

In ``Mode.ONLY`` the stored set is the allow-list, so blocking removes from it
and allowing adds to it.  In ``Mode.BLOCK`` the stored set is the block-list,
so the operations are the other way around.  The mode itself is never changed
by these helpers.
"""

from __future__ import annotations

from dataclasses import replace

from .model import Mode, Selection


def block_codes(selection: Selection, codes: frozenset[str]) -> Selection:
    """Return *selection* with every code in *codes* blocked."""
    current = selection.normalized()
    if current.mode is Mode.BLOCK:
        pops = current.pops | codes
    else:
        pops = current.pops - codes
    return replace(current, pops=frozenset(pops))


def allow_codes(selection: Selection, codes: frozenset[str]) -> Selection:
    """Return *selection* with every code in *codes* allowed."""
    current = selection.normalized()
    if current.mode is Mode.BLOCK:
        pops = current.pops - codes
    else:
        pops = current.pops | codes
    return replace(current, pops=frozenset(pops))
