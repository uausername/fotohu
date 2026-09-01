"""Tiny translation layer. Russian is the default; English is the fallback."""

from __future__ import annotations

from .en import TEXTS as EN
from .ru import TEXTS as RU

CATALOGS = {"ru": RU, "en": EN}
DEFAULT = "ru"


def t(lang: str | None, key: str, **kwargs: object) -> str:
    catalog = CATALOGS.get((lang or DEFAULT).lower(), RU)
    template = catalog.get(key) or EN.get(key) or RU.get(key) or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


__all__ = ["t", "CATALOGS", "DEFAULT"]
