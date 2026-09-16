"""Making a chat-sized picture out of an archived original.

The archive keeps the bytes the camera produced; the family chat cannot show
them. Telegram refuses photos over 10 MB or with wild dimensions, and a phone
screen has no use for 60 megapixels anyway — so the copy the family sees is a
small JPEG built here, while the copy in the cloud stays untouched.

Memory is the constraint that shapes this file. Everything else in FotoHu
streams bytes past without ever looking at them; a preview has to decode the
picture, and a decoded 50-megapixel photo is ~150 MB of pixels before any
resizing. The bot is expected to run on a 1 GB VPS, so the decoder is asked to
scale the image down *while* reading it rather than after.

Nothing in this module writes to the original: it is opened read-only, and the
preview is always a new file.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import images  # noqa: F401 - imported for its side effect: HEIC support

log = logging.getLogger(__name__)

#: Longest side of the preview, in pixels. Telegram re-encodes anything it is
#: sent anyway, so there is no point handing it more than a screen can show.
MAX_SIDE = 2560

JPEG_QUALITY = 85


def make_preview(source: Path, dest: Path, max_side: int = MAX_SIDE) -> Path | None:
    """Write a chat-sized JPEG of ``source``; ``None`` if it is not an image.

    A video, a PDF or a HEIC that Pillow cannot decode all return ``None`` — the
    caller simply has nothing to show, which is not an error for the upload.
    """
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return None

    try:
        with Image.open(source) as img:
            # Ask the decoder for a smaller picture *before* it decodes one.
            # A JPEG can be unpacked at half, quarter or eighth scale, and on a
            # 50-megapixel photo that is the difference between a few megabytes
            # and a few hundred — which on a 1 GB VPS is the difference between
            # a preview and an OOM kill. Mode is left alone (``None``) so a
            # grayscale scan stays grayscale; formats that cannot do this ignore
            # the request.
            img.draft(None, (max_side, max_side))
            img.load()
            # EXIF says which way is up; a preview has no EXIF, so rotate now or
            # half the family's portraits arrive lying on their side. In place,
            # because the alternative is a second full-size copy of the very
            # thing this file exists to keep small.
            ImageOps.exif_transpose(img, in_place=True)
            image = img if img.mode in ("RGB", "L") else img.convert("RGB")
            image.thumbnail((max_side, max_side), Image.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # No exif= argument: the preview deliberately carries no GPS trail.
            image.save(dest, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.debug("no preview for %s: %s", source.name, exc)
        dest.unlink(missing_ok=True)
        return None
    except Exception as exc:  # noqa: BLE001 - a broken image must not fail an upload
        log.warning("preview of %s failed: %s", source.name, exc)
        dest.unlink(missing_ok=True)
        return None
    return dest


__all__ = ["make_preview", "MAX_SIDE", "JPEG_QUALITY"]
