"""Turning a person + a timestamp + an original filename into a remote path.

Everything here is pure so the folder-layout matrix can be unit-tested without a
database or a cloud account.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from .models import FolderMode, Group, Person

# OneDrive/SharePoint reject these outright; the rest of the world dislikes them too.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_NAME_LEN = 120

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "ie",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "i", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
    }
)


def slugify(value: str, fallback: str = "user") -> str:
    """A conservative folder name: ASCII, no spaces, no punctuation."""
    text = value.strip().lower().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or fallback


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Strip anything that could escape the target folder or upset a provider."""
    # Defeat traversal before anything else: keep only the basename.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _ILLEGAL.sub("_", name).strip().strip(".")
    if not name:
        return fallback

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if len(stem) > MAX_NAME_LEN:
        stem = stem[:MAX_NAME_LEN]
    return f"{stem}.{ext}" if ext else stem


def sanitize_path(path: str) -> str:
    """Sanitize each segment of a ``a/b/c`` remote path and drop empty ones."""
    segments = path.replace("\\", "/").split("/")
    parts = [sanitize_filename(p) for p in segments if p not in ("", ".", "..")]
    return "/".join(p for p in parts if p)


def owner_segment(
    person: Person,
    group: Group | None,
    default_mode: FolderMode,
) -> str:
    """The per-owner part of the path — '' when everyone shares one folder.

    Resolution order matches the admin UI: an override on the person wins, then
    the person's group, then the global default.
    """
    mode = person.folder_mode_override or default_mode

    if mode == FolderMode.SHARED:
        return ""
    if mode == FolderMode.PER_GROUP:
        if group is not None:
            return slugify(group.folder or group.name)
        # No group assigned: fall back to a personal folder rather than dumping
        # this person's photos into everyone else's shared root.
        return person.personal_folder or slugify(person.name)
    return person.personal_folder or slugify(person.name)


def build_context(
    *,
    root: str,
    owner: str,
    taken_at: datetime,
    filename: str,
    person: Person,
    group: Group | None,
    compressed: bool = False,
) -> dict[str, str]:
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    return {
        "root": root.strip("/"),
        "owner": owner,
        "person": person.personal_folder or slugify(person.name),
        "group": slugify(group.folder or group.name) if group else "",
        "yyyy": f"{taken_at.year:04d}",
        "mm": f"{taken_at.month:02d}",
        "dd": f"{taken_at.day:02d}",
        "yyyy-mm": f"{taken_at.year:04d}-{taken_at.month:02d}",
        "yyyy-mm-dd": taken_at.strftime("%Y-%m-%d"),
        "hhmmss": taken_at.strftime("%H%M%S"),
        "filename": filename,
        "stem": stem,
        "ext": ext,
        "quality": "_compressed" if compressed else "",
    }


def render_template(template: str, ctx: dict[str, str]) -> str:
    """Expand ``{placeholders}``; unknown ones are left alone rather than raising."""

    def replace(match: re.Match[str]) -> str:
        return ctx.get(match.group(1), match.group(0))

    return re.sub(r"\{([a-z0-9_-]+)\}", replace, template)


DEFAULT_DIR_TEMPLATE = "{root}/{owner}/{quality}/{yyyy}/{yyyy-mm}"
DEFAULT_FILE_TEMPLATE = "{filename}"


def build_remote_dir(template: str, ctx: dict[str, str]) -> str:
    return sanitize_path(render_template(template, ctx))


def build_remote_filename(template: str, ctx: dict[str, str]) -> str:
    return sanitize_filename(render_template(template, ctx))


def dedupe_filename(filename: str, attempt: int) -> str:
    """``photo.jpg`` -> ``photo (2).jpg`` for the second attempt, and so on."""
    if attempt <= 1:
        return filename
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        return f"{filename} ({attempt})"
    return f"{stem} ({attempt}).{ext}"
