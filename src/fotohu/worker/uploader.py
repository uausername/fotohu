"""Background worker that drains the upload queue."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from ..core import pipeline
from ..core.errors import (
    FileTooLarge,
    IntegrityError,
    QuotaExceeded,
    RetryableError,
    StorageAuthError,
)
from ..core.models import Platform, UploadState
from ..db.repo import Repo
from ..i18n import t
from ..messengers.base import MessengerAdapter
from ..services.settings import SettingsService
from ..storage.registry import StorageRegistry

log = logging.getLogger(__name__)

IDLE_SLEEP = 2.0
#: Attempt N waits this many minutes. Beyond the list the row stays failed and
#: the admin can requeue it from the panel.
BACKOFF_MINUTES = [1, 5, 15, 60, 240]


class UploadWorker:
    def __init__(
        self,
        repo: Repo,
        settings_service: SettingsService,
        registry: StorageRegistry,
        adapters: dict[Platform, MessengerAdapter],
        temp_dir: Path,
        concurrency: int = 2,
    ) -> None:
        self.repo = repo
        self.settings_service = settings_service
        self.registry = registry
        self.adapters = adapters
        self.temp_dir = temp_dir
        self.concurrency = max(1, concurrency)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        #: Poked by the messenger handlers so a new photo starts uploading
        #: immediately instead of waiting out the idle sleep.
        self._wakeup = asyncio.Event()

    def notify(self) -> None:
        self._wakeup.set()

    async def start(self) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Pick up anything the previous run was in the middle of. Both of these
        # are leftovers by definition: no worker is running yet.
        recovered = await self.repo.recover_stuck_uploads()
        if recovered:
            log.info("requeued %d upload(s) interrupted by the last shutdown", recovered)
        self._clear_temp_files()

        self._tasks = [
            asyncio.create_task(self._loop(i), name=f"uploader-{i}")
            for i in range(self.concurrency)
        ]
        log.info("upload worker started (%d slots)", self.concurrency)

    def _clear_temp_files(self) -> None:
        """Drop half-downloaded files from an interrupted run.

        They are useless — the requeued upload downloads afresh — and on a small
        disk a few aborted videos add up.
        """
        for leftover in self.temp_dir.glob("upload-*"):
            try:
                leftover.unlink()
            except OSError as exc:  # noqa: PERF203 - one bad file must not stop startup
                log.warning("could not remove stale temp file %s: %s", leftover, exc)

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, slot: int) -> None:
        while not self._stop.is_set():
            try:
                record = await self.repo.claim_next_upload()
                if record is None:
                    self._wakeup.clear()
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=IDLE_SLEEP)
                    except TimeoutError:
                        pass
                    continue
                await self._handle(record)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("uploader slot %d crashed; continuing", slot)
                await asyncio.sleep(IDLE_SLEEP)

    async def _handle(self, record: dict) -> None:
        upload_id = record["id"]
        attempts = record["attempts"]
        settings = await self.settings_service.get()
        adapter = self.adapters.get(Platform(record["platform"]))
        if adapter is None:
            await self.repo.mark_failed(
                upload_id, f"no adapter for {record['platform']}", retry_in=None
            )
            return

        person = await self.repo.get_person(record["person_id"]) if record["person_id"] else None
        if person is None:
            await self.repo.mark_failed(upload_id, "sender is no longer registered", None)
            return
        group = await self.repo.get_group(person.group_id)

        storage_record = await self.repo.get_default_storage()
        if storage_record is None:
            await self._retry(upload_id, attempts, "no storage backend configured yet")
            await self._notify(adapter, record, t(settings.language, "err.no_storage"))
            return

        backend = await self.registry.build(storage_record)
        try:
            outcome = await pipeline.process_upload(
                record=record,
                repo=self.repo,
                adapter=adapter,
                backend=backend,
                storage_account_id=storage_record["id"],
                root_folder=storage_record["root_folder"] or settings.root_folder,
                settings=settings,
                person=person,
                group=group,
                temp_dir=self.temp_dir,
            )
        except FileTooLarge as exc:
            await self.repo.update_upload(
                upload_id, state=str(UploadState.REJECTED),
                last_error=f"too large: {exc.size} > {exc.limit}",
            )
            await self._notify(
                adapter, record,
                t(settings.language, "err.too_large", mb=round(exc.limit / 1024 / 1024)),
            )
            return
        except StorageAuthError as exc:
            # Nobody but an admin can fix this, so stop burning attempts on it.
            await self.repo.mark_failed(upload_id, str(exc), retry_in=timedelta(hours=1))
            log.error("storage auth failure: %s", exc)
            await self._alert_admins(t(settings.language, "err.storage_auth", error=str(exc)))
            return
        except QuotaExceeded as exc:
            await self.repo.mark_failed(upload_id, str(exc), retry_in=timedelta(hours=6))
            await self._alert_admins(t(settings.language, "err.quota"))
            return
        except (RetryableError, TimeoutError, OSError) as exc:
            await self._retry(upload_id, attempts, str(exc))
            return
        except IntegrityError as exc:
            # Do NOT purge the chat copy — it may be the only intact one left.
            await self.repo.mark_failed(upload_id, str(exc), retry_in=timedelta(minutes=15))
            await self._alert_admins(t(settings.language, "err.integrity", error=str(exc)))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("upload %s failed", upload_id)
            await self._retry(upload_id, attempts, f"{type(exc).__name__}: {exc}")
            return
        finally:
            await self.registry.persist_credentials(backend)
            await backend.close()

        await self._finish(record, outcome, settings, adapter, storage_record, person)

    async def _finish(
        self, record, outcome, settings, adapter, storage_record, person=None
    ) -> None:
        upload_id = record["id"]
        lang = settings.language

        if outcome.state == UploadState.REJECTED:
            await self.repo.update_upload(
                upload_id, state=str(UploadState.REJECTED), last_error=outcome.reason
            )
            await self._report(adapter, record, settings, t(lang, "quality.rejected"))
            return

        if outcome.state == UploadState.SKIPPED_DUP:
            await self.repo.update_upload(
                upload_id,
                state=str(UploadState.SKIPPED_DUP),
                remote_path=outcome.duplicate_of,
                # A duplicate is safely archived already, so its chat copy may go too.
                purge_after=_deadline_str(settings),
            )
            await self._report(
                adapter, record, settings,
                t(lang, "upload.duplicate", path=outcome.duplicate_of or ""),
            )
            return

        assert outcome.remote_file is not None
        await self.repo.mark_done(
            upload_id,
            remote_path=outcome.remote_file.path,
            remote_id=outcome.remote_file.remote_id,
            backend=storage_record["backend"],
            storage_account_id=storage_record["id"],
            verified=bool(outcome.verified),
            purge_after=pipeline.purge_deadline(settings),
        )
        # Every failure path logs; success did not, which left the log showing
        # only what went wrong and no way to confirm a file actually landed.
        log.info(
            "upload %s stored: %s (%s)",
            upload_id,
            outcome.remote_file.path,
            "hash verified" if outcome.verified else "size only",
        )
        key = "upload.ok" if outcome.verified else "upload.ok_unverified"
        await self._report(
            adapter, record, settings, t(lang, key, path=outcome.remote_file.path)
        )
        await self._announce_upload(record, person, settings, outcome.remote_file.path)

    async def _report(self, adapter, record: dict, settings, text: str) -> None:
        """One reply per file — but one summary per album.

        A ten-photo album would otherwise produce ten confirmations, each of which
        we would then have to delete again during the purge sweep.
        """
        group_id = record.get("media_group_id")
        if not group_id:
            await self._notify(adapter, record, text)
            return

        progress = await self.repo.media_group_progress(group_id)
        if not progress["finished"]:
            return  # a sibling is still uploading; the last one speaks for all

        parts = [t(settings.language, "album.done", n=progress["done"])]
        if progress["duplicates"]:
            parts.append(t(settings.language, "album.duplicates", n=progress["duplicates"]))
        if progress["rejected"]:
            parts.append(t(settings.language, "album.rejected", n=progress["rejected"]))
        if progress["failed"]:
            parts.append(t(settings.language, "album.failed", n=progress["failed"]))
        await self._notify(adapter, record, "\n".join(parts))

    async def _retry(self, upload_id: int, attempts: int, error: str) -> None:
        index = min(max(attempts - 1, 0), len(BACKOFF_MINUTES) - 1)
        delay = timedelta(minutes=BACKOFF_MINUTES[index])
        if attempts >= 6:
            await self.repo.mark_failed(upload_id, error, retry_in=None)
            log.error("upload %s given up after %d attempts: %s", upload_id, attempts, error)
            return
        await self.repo.mark_failed(upload_id, error, retry_in=delay)
        log.warning(
            "upload %s attempt %d failed (%s); retrying in %s",
            upload_id, attempts, error, delay,
        )

    async def _notify(self, adapter: MessengerAdapter, record: dict, text: str) -> None:
        try:
            message_id = await adapter.send_text(
                record["chat_id"], text, reply_to=record["message_id"]
            )
        except Exception as exc:  # noqa: BLE001 - a failed reply must not fail the upload
            log.warning("could not reply in %s: %s", record["chat_id"], exc)
            return
        if message_id:
            await self.repo.update_upload(record["id"], bot_message_id=str(message_id))

    async def _alert_admins(self, text: str, *, exclude_person_id: int | None = None) -> None:
        for account in await self.repo.list_admin_accounts():
            if exclude_person_id is not None and account.person_id == exclude_person_id:
                continue  # don't tell an admin about their own action
            adapter = self.adapters.get(account.platform)
            if adapter is None or not account.chat_id:
                continue
            try:
                await adapter.send_text(account.chat_id, text)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not alert admin %s: %s", account.platform_user_id, exc)

    async def _announce_upload(self, record: dict, person, settings, remote_path: str) -> None:
        """Tell the admins a photo just landed, if they asked to be told.

        One message per single photo; one per album, sent when the last sibling
        finishes — same batching rule the sender's own confirmation follows.
        """
        if not settings.notify_admin_on_upload:
            return
        who = person.name if person else "?"
        group_id = record.get("media_group_id")
        if group_id:
            progress = await self.repo.media_group_progress(group_id)
            if not progress["finished"]:
                return
            text = t(settings.language, "admin.new_album", name=who, n=progress["done"])
        else:
            text = t(settings.language, "admin.new_upload", name=who, path=remote_path)
        await self._alert_admins(text, exclude_person_id=record.get("person_id"))


def _deadline_str(settings) -> str | None:
    deadline = pipeline.purge_deadline(settings)
    return deadline.strftime("%Y-%m-%d %H:%M:%S") if deadline else None
