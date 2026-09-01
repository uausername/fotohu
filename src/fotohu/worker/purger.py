"""Removes archived photos from the messenger.

The rules this worker exists to respect:

* it only ever touches messages whose upload is ``done`` — the cloud copy must
  exist before the chat copy goes away;
* Telegram refuses ``deleteMessage`` for anything older than 48 h, so a message
  that slipped past that window is recorded as ``purge_error`` and reported in the
  admin panel rather than retried forever;
* Viber has no deletion API at all, so those rows are marked once and skipped.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from ..core.models import Platform
from ..db.repo import Repo
from ..messengers.base import MessengerAdapter
from ..services.settings import TELEGRAM_DELETE_WINDOW_HOURS, SettingsService

log = logging.getLogger(__name__)

SWEEP_INTERVAL = 300  # seconds
BATCH = 100           # Telegram's deleteMessages cap


class PurgeWorker:
    def __init__(
        self,
        repo: Repo,
        settings_service: SettingsService,
        adapters: dict[Platform, MessengerAdapter],
        interval: int = SWEEP_INTERVAL,
    ) -> None:
        self.repo = repo
        self.settings_service = settings_service
        self.adapters = adapters
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="purger")
        log.info("purge worker started (every %ds)", self.interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("purge sweep failed; will try again")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    async def sweep(self) -> int:
        """One pass. Returns how many chat messages were actually removed."""
        settings = await self.settings_service.get()
        if not settings.purge_enabled:
            return 0

        due = await self.repo.due_for_purge()
        if not due:
            return 0

        # Group by (platform, chat) so each batch is one API call.
        buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in due:
            buckets[(row["platform"], row["chat_id"])].append(row)

        removed = 0
        for (platform_name, chat_id), rows in buckets.items():
            adapter = self.adapters.get(Platform(platform_name))
            if adapter is None:
                continue
            if not adapter.supports_deletion:
                await self.repo.mark_purge_failed(
                    [r["id"] for r in rows],
                    f"{platform_name} has no message-deletion API",
                )
                continue
            removed += await self._purge_chat(adapter, chat_id, rows, settings)
        return removed

    async def _purge_chat(
        self, adapter: MessengerAdapter, chat_id: str, rows: list[dict], settings
    ) -> int:
        window = adapter.delete_window_hours
        fresh: list[dict] = []
        stale: list[dict] = []
        now = datetime.now()

        for row in rows:
            if window is not None and _age(row, now) > timedelta(hours=window):
                stale.append(row)
            else:
                fresh.append(row)

        if stale:
            await self.repo.mark_purge_failed(
                [r["id"] for r in stale],
                f"older than {window}h — {adapter.platform} refuses to delete it",
            )
            log.warning(
                "%d message(s) in %s aged past the %dh deletion window; "
                "lower purge_after_hours to below %d to stop this recurring",
                len(stale), chat_id, window, TELEGRAM_DELETE_WINDOW_HOURS,
            )

        removed = 0
        for chunk in _chunks(fresh, BATCH):
            # Our own confirmation replies go with the photo they refer to.
            targets: list[str] = []
            owner: dict[str, list[int]] = defaultdict(list)
            for row in chunk:
                targets.append(str(row["message_id"]))
                owner[str(row["message_id"])].append(row["id"])
                if settings.purge_bot_replies and row.get("bot_message_id"):
                    targets.append(str(row["bot_message_id"]))
                    owner[str(row["bot_message_id"])].append(row["id"])

            result = await adapter.delete_messages(chat_id, targets)

            done_ids: set[int] = set()
            for message_id in result.deleted:
                done_ids.update(owner.get(message_id, []))
            failed_ids: dict[int, str] = {}
            for message_id, reason in result.failed.items():
                for upload_id in owner.get(message_id, []):
                    failed_ids[upload_id] = reason

            # A row counts as purged only if nothing about it failed.
            purged = sorted(done_ids - set(failed_ids))
            await self.repo.mark_purged(purged)
            removed += len(purged)
            for upload_id, reason in failed_ids.items():
                await self.repo.mark_purge_failed([upload_id], reason)

        return removed


def _age(row: dict, now: datetime) -> timedelta:
    """How long ago the *user* sent the message — that is what the API measures."""
    raw = row.get("received_at")
    if not raw:
        return timedelta(0)
    try:
        return now - datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return timedelta(0)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
