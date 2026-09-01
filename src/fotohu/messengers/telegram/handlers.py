"""Telegram command and media handlers for ordinary family members."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from ...context import AppContext
from ...core.models import IncomingMedia, Platform, SourceKind
from ...i18n import t
from .adapter import PUBLIC_API_DOWNLOAD_LIMIT

log = logging.getLogger(__name__)

router = Router(name="member")


def human_size(num: int | None) -> str:
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _display_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "unknown"
    return user.full_name or user.username or str(user.id)


async def _lang(ctx: AppContext) -> str:
    return (await ctx.settings.get()).language


# --------------------------------------------------------------------- commands


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, ctx: AppContext, command: CommandObject) -> None:
    lang = await _lang(ctx)
    user = message.from_user
    assert user is not None
    uid, chat_id = str(user.id), str(message.chat.id)

    known = await ctx.members.resolve(Platform.TELEGRAM, uid)
    if known:
        person, _ = known
        await ctx.members.touch_account(person, Platform.TELEGRAM, uid, user.username, chat_id)
        folder = await ctx.members.folder_preview(person)
        await message.answer(t(lang, "start.known", name=person.name, folder=folder))
        return

    # /start <token> claims the first admin seat on a fresh install.
    token = (command.args or "").strip()
    if token:
        result = await ctx.members.bootstrap_admin(
            token, ctx.config.bootstrap_token, Platform.TELEGRAM, uid,
            _display_name(message), chat_id,
        )
        if result.ok:
            await message.answer(t(lang, "start.bootstrap"))
            return
        # Not the bootstrap token? Maybe they pasted an invite code into /start.
        joined = await ctx.members.join_with_code(
            token, Platform.TELEGRAM, uid, _display_name(message), chat_id
        )
        if joined.ok and joined.person:
            folder = await ctx.members.folder_preview(joined.person)
            await message.answer(t(lang, "join.ok", name=joined.person.name, folder=folder))
            return

    await message.answer(t(lang, "start.unknown"))


@router.message(Command("join"))
async def cmd_join(message: Message, ctx: AppContext, command: CommandObject) -> None:
    lang = await _lang(ctx)
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer(t(lang, "join.usage"))
        return

    user = message.from_user
    assert user is not None
    result = await ctx.members.join_with_code(
        code, Platform.TELEGRAM, str(user.id), _display_name(message), str(message.chat.id)
    )
    if result.reason == "already_member":
        await message.answer(t(lang, "join.already"))
        return
    if not result.ok or not result.person:
        await message.answer(t(lang, "join.bad_code"))
        return
    folder = await ctx.members.folder_preview(result.person)
    await message.answer(t(lang, "join.ok", name=result.person.name, folder=folder))


@router.message(Command("howto"))
async def cmd_howto(message: Message, ctx: AppContext) -> None:
    await message.answer(t(await _lang(ctx), "howto"))


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: AppContext) -> None:
    lang = await _lang(ctx)
    user = message.from_user
    assert user is not None
    text = t(lang, "help.member")
    known = await ctx.members.resolve(Platform.TELEGRAM, str(user.id))
    if known and known[0].is_admin:
        text += t(lang, "help.admin_extra")
    await message.answer(text)


@router.message(Command("me"))
async def cmd_me(message: Message, ctx: AppContext) -> None:
    lang = await _lang(ctx)
    user = message.from_user
    assert user is not None
    known = await ctx.members.resolve(Platform.TELEGRAM, str(user.id))
    if not known:
        await message.answer(t(lang, "access.denied"))
        return
    person, _ = known
    stats = (await ctx.repo.per_person_stats()).get(person.id, {"count": 0, "bytes": 0})
    await message.answer(
        t(
            lang, "me",
            name=person.name,
            role=person.role,
            folder=await ctx.members.folder_preview(person),
            count=stats["count"],
            size=human_size(stats["bytes"]),
        )
    )


@router.message(Command("last"))
async def cmd_last(message: Message, ctx: AppContext) -> None:
    lang = await _lang(ctx)
    user = message.from_user
    assert user is not None
    known = await ctx.members.resolve(Platform.TELEGRAM, str(user.id))
    if not known:
        await message.answer(t(lang, "access.denied"))
        return

    rows = await ctx.repo.recent_uploads(person_id=known[0].id, limit=10)
    if not rows:
        await message.answer(t(lang, "last.empty"))
        return

    icons = {"done": "✅", "pending": "⏳", "uploading": "⏫", "failed": "❌",
             "skipped_dup": "♻️", "rejected": "⚠️"}
    lines = [t(lang, "last.header")]
    for row in rows:
        lines.append(
            f"{icons.get(row['state'], '•')} <code>{row['remote_path'] or row['file_name']}</code>"
        )
    await message.answer("\n".join(lines))


# ------------------------------------------------------------------ media intake


@router.message(F.document | F.photo | F.video | F.video_note | F.animation)
async def on_media(message: Message, ctx: AppContext) -> None:
    lang = await _lang(ctx)
    user = message.from_user
    assert user is not None
    uid = str(user.id)

    known = await ctx.members.resolve(Platform.TELEGRAM, uid)
    if not known:
        await message.answer(t(lang, "access.denied"))
        return
    person, _ = known
    if not person.is_active:
        await message.answer(t(lang, "access.blocked"))
        return
    await ctx.members.touch_account(
        person, Platform.TELEGRAM, uid, user.username, str(message.chat.id)
    )

    media = _extract(message)
    if media is None:
        return

    adapter = ctx.adapters.get(Platform.TELEGRAM)
    if (
        adapter is not None
        and adapter.download_limit
        and media.size
        and media.size > adapter.download_limit
    ):
        # Say this now rather than letting the worker discover it: the user is
        # standing there waiting, and the fix is on the admin's side.
        await message.answer(t(lang, "err.telegram_20mb"))
        return

    upload_id = await ctx.repo.create_upload(media, person.id)
    if upload_id is None:
        return  # Telegram redelivered an update we already queued

    settings = await ctx.settings.get()
    if not media.lossless and settings.photo_policy == "reject":
        # Answer immediately — the worker would only repeat it a second later.
        await ctx.repo.update_upload(upload_id, state="rejected", last_error="compressed")
        await message.answer(t(lang, "quality.rejected"))
        return

    if not media.media_group_id:
        await message.answer(t(lang, "upload.queued", name=media.file_name))
    if ctx.uploader:
        ctx.uploader.notify()


def _extract(message: Message) -> IncomingMedia | None:
    """Map an aiogram message onto our platform-neutral media type."""
    common = {
        "platform": Platform.TELEGRAM,
        "chat_id": str(message.chat.id),
        "message_id": str(message.message_id),
        "media_group_id": message.media_group_id,
        "caption": message.caption,
        "sent_at": message.date.replace(tzinfo=None) if message.date else None,
    }

    if doc := message.document:
        return IncomingMedia(
            source_kind=SourceKind.DOCUMENT,
            file_name=doc.file_name or f"document_{message.message_id}",
            file_ref=doc.file_id,
            size=doc.file_size,
            mime_type=doc.mime_type,
            **common,
        )

    if message.photo:
        # photo[] is ordered smallest-first; the last entry is the largest one
        # Telegram kept — still a re-encode, never the original.
        largest = message.photo[-1]
        return IncomingMedia(
            source_kind=SourceKind.PHOTO,
            file_name=f"photo_{message.message_id}.jpg",
            file_ref=largest.file_id,
            size=largest.file_size,
            mime_type="image/jpeg",
            **common,
        )

    if video := message.video:
        return IncomingMedia(
            source_kind=SourceKind.VIDEO,
            file_name=video.file_name or f"video_{message.message_id}.mp4",
            file_ref=video.file_id,
            size=video.file_size,
            mime_type=video.mime_type or "video/mp4",
            **common,
        )

    if animation := message.animation:
        return IncomingMedia(
            source_kind=SourceKind.VIDEO,
            file_name=animation.file_name or f"animation_{message.message_id}.mp4",
            file_ref=animation.file_id,
            size=animation.file_size,
            mime_type=animation.mime_type or "video/mp4",
            **common,
        )

    if note := message.video_note:
        return IncomingMedia(
            source_kind=SourceKind.VIDEO,
            file_name=f"video_note_{message.message_id}.mp4",
            file_ref=note.file_id,
            size=note.file_size,
            mime_type="video/mp4",
            **common,
        )

    return None


__all__ = ["router", "human_size", "PUBLIC_API_DOWNLOAD_LIMIT"]
