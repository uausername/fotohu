"""The shared feed: showing the family a photo the moment it is archived.

The archive is not a conversation. Files go up, the chat is swept clean, and
nobody sees what anyone else sent — which is exactly the sense of a shared
family album that the bot was supposed to create. This service puts the moment
back: as soon as a photo is verified in the cloud, everyone else gets it in
their chat, as a picture, with the sender's name on it.

Three rules hold it together:

* **it is a view, never a second archive.** A mirror is a bot message tracked in
  its own table; nothing about it can be claimed by the upload queue, so showing
  one photo to five people still means exactly one file in the cloud;
* **it goes out only after the cloud copy is verified**, so the feed never
  advertises a photo the archive does not actually hold;
* **it leaves the way the original does.** Each mirror carries the same purge
  deadline as the upload it came from, and the same sweep takes it away.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from pathlib import Path

from ..core.errors import RetryableError
from ..core.models import Person, Platform
from ..core.preview import make_preview
from ..db.repo import Repo
from ..i18n import t
from ..messengers.base import MessengerAdapter

log = logging.getLogger(__name__)

#: Original captions can be long; the feed shows the beginning and stops there.
CAPTION_TEXT_LIMIT = 300

#: How long we are willing to sit out Telegram's flood control before giving up
#: on one chat. Longer than any pause it normally asks for, short enough that a
#: worker slot is never parked on it.
MAX_FLOOD_WAIT = 60.0

#: Used when the refusal carries no number of its own.
FLOOD_FALLBACK_WAIT = 5.0


class MirrorService:
    def __init__(
        self,
        repo: Repo,
        adapters: dict[Platform, MessengerAdapter],
        temp_dir: Path,
    ) -> None:
        self.repo = repo
        self.adapters = adapters
        self.temp_dir = temp_dir

    async def fan_out(
        self,
        *,
        record: dict,
        person: Person | None,
        settings,
        source: Path,
        purge_after: datetime | None,
    ) -> int:
        """Show one archived photo to everyone else. Returns how many saw it.

        A chat that refuses the picture — blocked bot, flood control, someone who
        deleted the conversation — costs only that chat: the loop moves on and the
        upload is untouched. Showing a photo is a courtesy; archiving it is the job.
        """
        if not settings.mirror_enabled:
            return 0

        targets = await self._targets(record, person)
        if not targets:
            return 0

        preview = self.temp_dir / f"mirror-{record['id']}.jpg"
        if make_preview(source, preview) is None:
            # Not a picture Pillow can read — a video, a RAW, a document. Said
            # out loud, because from the outside "the feed skipped this one" and
            # "the feed is broken" look identical.
            log.info(
                "nothing to show for %s: %s is not an image we can read",
                record.get("file_name"), source.suffix or "this file",
            )
            return 0

        caption = self._caption(record, person, settings)
        shown = 0
        # The first recipient carries the bytes; everyone after them rides the
        # handle Telegram gives back for the very same picture.
        handle: str | None = None
        try:
            for target in targets:
                adapter = self.adapters[Platform(target["platform"])]
                try:
                    sent = await self._send(
                        adapter, target["chat_id"], handle or preview, caption
                    )
                except Exception as exc:  # noqa: BLE001 - one bad chat, not a bad upload
                    log.warning(
                        "could not show %s in %s: %s",
                        record.get("file_name"), target["chat_id"], exc,
                    )
                    continue
                if sent is None:
                    continue
                handle = handle or sent.reusable_ref
                await self.repo.record_mirror(
                    upload_id=record["id"],
                    person_id=target["person_id"],
                    platform=target["platform"],
                    chat_id=target["chat_id"],
                    message_id=sent.message_id,
                    purge_after=purge_after,
                )
                shown += 1
        finally:
            preview.unlink(missing_ok=True)

        if shown:
            log.info("showed %s to %d chat(s)", record.get("file_name"), shown)
        return shown

    async def _send(self, adapter: MessengerAdapter, chat_id: str, photo, caption: str):
        """Send one photo, sitting out flood control once if Telegram asks for it.

        An album is what provokes it: ten photos into the same chat, each the
        moment its upload lands. Telegram answers "not so fast, wait N seconds",
        and dropping the rest of the album on that refusal is precisely how a
        working feature comes to look broken — the sender sees nothing arrive
        and the log says one word about flooding.
        """
        try:
            return await adapter.send_photo(chat_id, photo, caption)
        except RetryableError as exc:
            delay = min(exc.retry_after or FLOOD_FALLBACK_WAIT, MAX_FLOOD_WAIT)
            log.info(
                "flood control in %s; waiting %.0fs and trying once more", chat_id, delay
            )
            await asyncio.sleep(delay)
            return await adapter.send_photo(chat_id, photo, caption)

    async def _targets(self, record: dict, person: Person | None) -> list[dict]:
        """Who still needs to see this photo, each chat at most once.

        Skipped here: the sender (they are looking at their own message), the
        chat the photo came from, any chat that already has this very upload,
        and any messenger that cannot post a picture at all.
        """
        seen = await self.repo.mirrored_chats(record["id"])
        seen.add((str(record["platform"]), str(record["chat_id"])))
        targets = []
        for row in await self.repo.mirror_targets(
            exclude_person_id=person.id if person else None
        ):
            key = (str(row["platform"]), str(row["chat_id"]))
            if key in seen:
                continue
            adapter = self.adapters.get(Platform(row["platform"]))
            if adapter is None or not adapter.supports_photo:
                continue
            seen.add(key)
            targets.append(row)
        return targets

    def _caption(self, record: dict, person: Person | None, settings) -> str:
        """Who sent it, and what they wrote — escaped: names are not our markup."""
        name = html.escape(person.name if person else "?", quote=False)
        note = (record.get("caption") or "").strip()
        if not note:
            return t(settings.language, "mirror.caption", name=name)
        return t(
            settings.language,
            "mirror.caption_with_note",
            name=name,
            note=html.escape(note[:CAPTION_TEXT_LIMIT], quote=False),
        )


__all__ = ["MirrorService"]
