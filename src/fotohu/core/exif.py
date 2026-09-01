"""Read-only EXIF inspection.

We never write to the file: the whole point of the project is that the bytes that
land in the cloud are the bytes the camera produced.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# EXIF tag ids, from the spec. Preferred order: original > digitised > file change.
_DATETIME_ORIGINAL = 0x9003
_DATETIME_DIGITIZED = 0x9004
_DATETIME = 0x0132

_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("\x00")
    # A camera that has never had its clock set writes all zeroes.
    if not text or text.startswith("0000"):
        return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def read_taken_at(path: Path) -> datetime | None:
    """Best-effort capture time. Returns ``None`` for anything non-JPEG/TIFF-ish."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return None

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            for tag in (_DATETIME_ORIGINAL, _DATETIME_DIGITIZED, _DATETIME):
                # Sub-IFD 0x8769 holds DateTimeOriginal on most cameras.
                for source in (exif.get_ifd(0x8769), exif):
                    if not source:
                        continue
                    parsed = _parse(source.get(tag))
                    if parsed:
                        return parsed
    except (UnidentifiedImageError, OSError, ValueError, KeyError) as exc:
        log.debug("no EXIF from %s: %s", path.name, exc)
    except Exception as exc:  # noqa: BLE001 - a broken file must not kill an upload
        log.debug("EXIF read failed for %s: %s", path.name, exc)
    return None


def resolve_taken_at(
    local_path: Path | None,
    message_sent_at: datetime | None,
    prefer_exif: bool = True,
) -> tuple[datetime, str]:
    """Return ``(timestamp, source)`` where source is exif | message | now."""
    if prefer_exif and local_path is not None:
        taken = read_taken_at(local_path)
        if taken is not None:
            return taken, "exif"
    if message_sent_at is not None:
        return message_sent_at.replace(tzinfo=None), "message"
    return datetime.now(), "now"
