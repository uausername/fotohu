"""Telegram side of :class:`MessengerAdapter`.

Two Telegram facts shape this file:

* the public Bot API refuses ``getFile`` for anything over 20 MB, while a
  self-hosted ``telegram-bot-api`` lifts that to 2 GB and hands back a path on
  local disk instead of a URL — so we support both;
* ``deleteMessage`` only works within 48 hours of the message being sent.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from ...core.download import copy_into, stream_to_file
from ...core.errors import DownloadError, FileTooLarge, RetryableError
from ...core.models import LocalFile, Platform
from ..base import DeleteResult, MessengerAdapter

log = logging.getLogger(__name__)

#: Hard ceiling of the public Bot API for bot-side downloads.
PUBLIC_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024

#: Telegram will not delete anything older than this, whatever rights the bot has.
DELETE_WINDOW_HOURS = 48


def _reclaim(path: Path) -> None:
    """Drop the local API server's copy once we have taken our own.

    A self-hosted ``telegram-bot-api`` never reclaims what it downloads: in
    local mode that is the bot's job, and nothing else will ever read this file.
    Left alone it fills the disk one video at a time.

    Failing is not fatal — we already hold the bytes we came for, and the worst
    case is the disk usage we had before this existed — but it is worth a line
    in the log, because the likely cause is the two containers running as
    different users and needs a person to fix.
    """
    try:
        path.unlink()
    except OSError as exc:
        log.warning("could not reclaim %s from the local API server: %s", path, exc)

#: deleteMessages (plural) takes at most this many ids per call.
DELETE_BATCH = 100


class TelegramAdapter(MessengerAdapter):
    platform = Platform.TELEGRAM
    supports_deletion = True
    delete_window_hours = DELETE_WINDOW_HOURS

    def __init__(self, bot: Bot, local_mode: bool = False) -> None:
        self.bot = bot
        self.local_mode = local_mode
        self.download_limit = None if local_mode else PUBLIC_API_DOWNLOAD_LIMIT

    async def download(
        self, file_ref: str, dest: Path, size_limit: int | None = None
    ) -> LocalFile:
        try:
            file = await self.bot.get_file(file_ref)
        except TelegramBadRequest as exc:
            if "file is too big" in str(exc).lower():
                raise FileTooLarge(0, PUBLIC_API_DOWNLOAD_LIMIT) from exc
            raise DownloadError(str(exc)) from exc

        if file.file_size and size_limit and file.file_size > size_limit:
            raise FileTooLarge(file.file_size, size_limit)

        if not file.file_path:
            raise DownloadError("Telegram returned no file_path")

        # A local Bot API server shares the filesystem with us: just copy.
        if self.local_mode and Path(file.file_path).is_absolute():
            source = Path(file.file_path)
            if not source.exists():
                # There is no HTTP to fall back to: with ``is_local`` set,
                # aiogram's downloader reads this very path off the disk, so a
                # stream here would only fail one frame deeper. Either the two
                # containers do not really share the volume, or the server
                # handed back a cached path for a file we have already reclaimed
                # — and the retry that follows will ask it for a fresh copy.
                raise DownloadError(
                    f"local mode is on but {source} is not there: check that the bot "
                    f"and telegram-bot-api share the same volume"
                )
            local = copy_into(source, dest)
            _reclaim(source)
            return local

        stream = await self.bot.download_file(file.file_path, chunk_size=256 * 1024)

        async def chunks():
            try:
                while data := stream.read(256 * 1024):
                    yield data
            finally:
                stream.close()

        return await stream_to_file(chunks(), dest, size_limit=size_limit)

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str | None:
        try:
            message = await self.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                reply_to_message_id=int(reply_to) if reply_to else None,
            )
        except TelegramBadRequest as exc:
            # The message being replied to may already be gone; send standalone.
            if reply_to and "reply" in str(exc).lower():
                message = await self.bot.send_message(chat_id=int(chat_id), text=text)
            else:
                raise
        except TelegramRetryAfter as exc:
            raise RetryableError(f"flood control: retry after {exc.retry_after}s") from exc
        return str(message.message_id)

    async def delete_messages(self, chat_id: str, message_ids: list[str]) -> DeleteResult:
        result = DeleteResult()
        ids = [int(m) for m in message_ids if str(m).lstrip("-").isdigit()]

        for start in range(0, len(ids), DELETE_BATCH):
            batch = ids[start : start + DELETE_BATCH]
            try:
                await self.bot.delete_messages(chat_id=int(chat_id), message_ids=batch)
                result.deleted.extend(str(i) for i in batch)
            except TelegramRetryAfter as exc:
                for i in batch:
                    result.failed[str(i)] = f"flood control, retry after {exc.retry_after}s"
            except TelegramBadRequest as exc:
                # A batch fails as a unit, so retry one by one to learn which
                # messages are actually past the 48-hour window.
                log.debug("batch delete rejected (%s); retrying individually", exc)
                for i in batch:
                    single = await self._delete_one(int(chat_id), i)
                    if single is None:
                        result.deleted.append(str(i))
                    else:
                        result.failed[str(i)] = single
        return result

    async def _delete_one(self, chat_id: int, message_id: int) -> str | None:
        """Delete one message; return ``None`` on success or a reason on failure."""
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return None
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "message to delete not found" in text:
                return None  # already gone; that is the outcome we wanted
            if "can't be deleted" in text or "too old" in text:
                return f"older than {DELETE_WINDOW_HOURS}h — Telegram refuses to delete it"
            return str(exc)[:200]
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"[:200]

    async def close(self) -> None:
        await self.bot.session.close()
