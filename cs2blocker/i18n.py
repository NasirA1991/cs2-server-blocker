"""Small gettext wrapper used by both the GUI and CLI.

Translations are optional.  The application ships the ``.pot`` template and
looks for compiled catalogs inside ``cs2blocker/locale``; when no catalog is
installed every lookup simply returns the original English string.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "cs2blocker"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
_translation: gettext.NullTranslations | None = None


def setup() -> None:
    """Bind the message domain once, before the UI is constructed."""
    global _translation
    try:
        _translation = gettext.translation(
            DOMAIN,
            localedir=str(LOCALE_DIR),
            fallback=True,
        )
    except Exception:
        _translation = gettext.NullTranslations()


def _(message: str) -> str:
    if _translation is None:
        setup()
    assert _translation is not None
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    if _translation is None:
        setup()
    assert _translation is not None
    return _translation.ngettext(singular, plural, count)
