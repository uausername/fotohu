"""Teach Pillow the formats a phone actually produces.

Pillow reads JPEG and PNG out of the box. It does not read HEIC — and HEIC is
exactly what an iPhone hands over when a photo is sent *as a file*, which is the
one thing this bot asks the family to do. The originals were never in danger:
nothing here touches the bytes on their way to the cloud. What was lost is
everything that requires looking *inside* the file — the capture date the photo
is filed by, and the preview the family sees in the shared feed. Both failed
quietly, one debug line apiece.

Registering the opener is global to Pillow and safe to repeat, so this module
does it once on import and both readers pull it in.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Formats we add to Pillow here, for the log line that says whether we managed.
EXTRA_FORMATS = ("HEIC", "HEIF")


def _register() -> bool:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:  # pragma: no cover - a declared dependency
        # Not fatal: photos still reach the cloud untouched. They just cannot be
        # dated from EXIF or shown in the feed, so say so rather than leaving
        # someone to wonder why iPhone photos behave differently.
        log.warning(
            "pillow-heif is not installed: HEIC photos will be archived as sent, "
            "but not dated from EXIF and not shown in the shared feed"
        )
        return False
    register_heif_opener()
    return True


REGISTERED = _register()

__all__ = ["REGISTERED", "EXTRA_FORMATS"]
