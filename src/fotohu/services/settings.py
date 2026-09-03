"""Runtime settings: everything an admin can change from inside the chat.

Defaults are chosen so a fresh install is already safe and lossless:
originals only, delete from the chat one hour after the copy is verified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from ..core.models import FolderMode, PhotoPolicy
from ..core.naming import DEFAULT_DIR_TEMPLATE, DEFAULT_FILE_TEMPLATE
from ..db.repo import Repo

#: Telegram refuses ``deleteMessage`` for anything older than this. It is a
#: server-side rule; no bot, and no self-hosted Bot API server, can get past it.
TELEGRAM_DELETE_WINDOW_HOURS = 48


@dataclass
class Settings:
    folder_mode: FolderMode = FolderMode.PER_PERSON
    root_folder: str = "FotoHu"
    dir_template: str = DEFAULT_DIR_TEMPLATE
    file_template: str = DEFAULT_FILE_TEMPLATE
    prefer_exif_date: bool = True

    photo_policy: PhotoPolicy = PhotoPolicy.REJECT

    purge_enabled: bool = True
    #: Hours to wait after a verified upload before deleting the chat message.
    #: Must stay under 48 for Telegram to accept the deletion at all.
    purge_after_hours: int = 1
    purge_bot_replies: bool = True
    #: Viber cannot delete messages; optionally nudge the sender to do it by hand.
    viber_purge_reminder: bool = False

    dedupe_enabled: bool = True
    verify_hashes: bool = True
    max_file_mb: int = 2048
    language: str = "ru"

    #: Ping every active admin in chat whenever a member archives a photo.
    #: Off by default so a fresh install does not spam the admin.
    notify_admin_on_upload: bool = False

    @property
    def purge_exceeds_telegram_window(self) -> bool:
        return self.purge_after_hours >= TELEGRAM_DELETE_WINDOW_HOURS


_ENUMS: dict[str, type] = {"folder_mode": FolderMode, "photo_policy": PhotoPolicy}


class SettingsService:
    """Reads/writes the ``settings`` table with a small in-process cache."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self._cache: Settings | None = None

    async def get(self) -> Settings:
        if self._cache is not None:
            return self._cache
        stored = await self.repo.all_settings()
        values: dict[str, Any] = {}
        for f in fields(Settings):
            if f.name not in stored:
                continue
            raw = stored[f.name]
            enum_type = _ENUMS.get(f.name)
            try:
                values[f.name] = enum_type(raw) if enum_type else raw
            except ValueError:
                continue  # stale value from an older version — keep the default
        self._cache = Settings(**values)
        return self._cache

    async def set(self, key: str, value: Any) -> Settings:
        valid = {f.name for f in fields(Settings)}
        if key not in valid:
            raise KeyError(f"unknown setting: {key}")
        if isinstance(value, FolderMode | PhotoPolicy):
            value = str(value)
        await self.repo.set_setting(key, value)
        self._cache = None
        return await self.get()

    async def update(self, **kwargs: Any) -> Settings:
        for key, value in kwargs.items():
            await self.set(key, value)
        return await self.get()

    def invalidate(self) -> None:
        self._cache = None

    async def as_dict(self) -> dict[str, Any]:
        settings = await self.get()
        return {k: (str(v) if hasattr(v, "value") else v) for k, v in asdict(settings).items()}
