"""Viber webhook event handling.

Viber has no inline keyboards worth building an admin panel on and no callback
model like Telegram's, so admins get the same operations as plain text commands,
driven by the shared :class:`AdminService`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...context import AppContext
from ...core.models import IncomingMedia, Platform, Role, SourceKind
from ...i18n import t
from ...services.admin import AdminService

log = logging.getLogger(__name__)


async def handle_event(ctx: AppContext, event: dict[str, Any]) -> None:
    kind = event.get("event")
    if kind == "conversation_started":
        await _on_conversation_started(ctx, event)
    elif kind == "message":
        await _on_message(ctx, event)
    elif kind == "unsubscribed":
        log.info("viber user %s unsubscribed", event.get("user_id"))


async def _on_conversation_started(ctx: AppContext, event: dict[str, Any]) -> None:
    user = event.get("user") or {}
    lang = (await ctx.settings.get()).language
    known = await ctx.members.resolve(Platform.VIBER, str(user.get("id", "")))
    client = ctx.adapters[Platform.VIBER]

    if known:
        person, _ = known
        folder = await ctx.members.folder_preview(person)
        await client.send_text(
            str(user["id"]), t(lang, "start.known", name=person.name, folder=folder)
        )
    else:
        await client.send_text(str(user["id"]), t(lang, "start.unknown"))


async def _on_message(ctx: AppContext, event: dict[str, Any]) -> None:
    sender = event.get("sender") or {}
    user_id = str(sender.get("id") or "")
    if not user_id:
        return

    message = event.get("message") or {}
    lang = (await ctx.settings.get()).language
    adapter = ctx.adapters[Platform.VIBER]
    name = sender.get("name") or user_id

    text = (message.get("text") or "").strip()
    known = await ctx.members.resolve(Platform.VIBER, user_id)

    # --- text commands ------------------------------------------------------
    if text.startswith("/"):
        await _handle_command(ctx, adapter, user_id, name, text, known, lang)
        return

    if not known:
        await adapter.send_text(user_id, t(lang, "access.denied"))
        return

    person, _ = known
    if not person.is_active:
        await adapter.send_text(user_id, t(lang, "access.blocked"))
        return
    await ctx.members.touch_account(person, Platform.VIBER, user_id, name, user_id)

    media = _extract(event, message)
    if media is None:
        if text:
            await adapter.send_text(user_id, t(lang, "help.member"))
        return

    settings = await ctx.settings.get()
    if not media.lossless and settings.photo_policy == "reject":
        await adapter.send_text(user_id, t(lang, "quality.rejected"))
        return

    upload_id = await ctx.repo.create_upload(media, person.id)
    if upload_id is None:
        return  # Viber retries deliveries; we already have this one
    await adapter.send_text(user_id, t(lang, "upload.queued", name=media.file_name))
    if ctx.uploader:
        ctx.uploader.notify()


async def _handle_command(ctx, adapter, user_id, name, text, known, lang) -> None:
    parts = text.split(maxsplit=1)
    command = parts[0].lower().lstrip("/")
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command == "join":
        if not argument:
            await adapter.send_text(user_id, t(lang, "join.usage"))
            return
        result = await ctx.members.join_with_code(
            argument.upper(), Platform.VIBER, user_id, name, user_id
        )
        if result.reason == "already_member":
            await adapter.send_text(user_id, t(lang, "join.already"))
            return
        if not result.ok or not result.person:
            await adapter.send_text(user_id, t(lang, "join.bad_code"))
            return
        folder = await ctx.members.folder_preview(result.person)
        await adapter.send_text(
            user_id, t(lang, "join.ok", name=result.person.name, folder=folder)
        )
        return

    if command == "start" and argument:
        result = await ctx.members.bootstrap_admin(
            argument, ctx.config.bootstrap_token, Platform.VIBER, user_id, name, user_id
        )
        if result.ok:
            await adapter.send_text(user_id, t(lang, "start.bootstrap"))
            return

    if command == "howto":
        await adapter.send_text(user_id, t(lang, "howto"))
        return

    if not known:
        await adapter.send_text(user_id, t(lang, "access.denied"))
        return
    person, _ = known

    if command in ("help", "start"):
        message = t(lang, "help.member")
        if person.is_admin:
            message += "\n\n" + _admin_help()
        await adapter.send_text(user_id, message)
        return

    if command == "me":
        stats = (await ctx.repo.per_person_stats()).get(person.id, {"count": 0, "bytes": 0})
        from ...services.admin import human_size

        await adapter.send_text(
            user_id,
            t(lang, "me", name=person.name, role=person.role,
              folder=await ctx.members.folder_preview(person),
              count=stats["count"], size=human_size(stats["bytes"])),
        )
        return

    if command == "last":
        rows = await ctx.repo.recent_uploads(person_id=person.id, limit=10)
        if not rows:
            await adapter.send_text(user_id, t(lang, "last.empty"))
            return
        lines = [t(lang, "last.header")]
        lines += [f"• {r['remote_path'] or r['file_name']} — {r['state']}" for r in rows]
        await adapter.send_text(user_id, "\n".join(lines))
        return

    # --- admin-only from here ----------------------------------------------
    if not person.is_admin:
        await adapter.send_text(user_id, t(lang, "access.admin_only"))
        return

    admin = AdminService(ctx.repo, ctx.settings, ctx.members, ctx.storage)

    if command in ("admin", "settings"):
        await adapter.send_text(user_id, _admin_help())
        return
    if command == "status":
        await adapter.send_text(user_id, await admin.status())
        return
    if command == "members":
        await adapter.send_text(user_id, await admin.family_overview())
        return
    if command == "groups":
        if argument:
            result = await admin.create_group(argument)
            await adapter.send_text(user_id, result.message)
            return
        await adapter.send_text(user_id, await admin.groups_overview())
        return
    if command == "storage":
        await adapter.send_text(user_id, await admin.storage_overview())
        return
    if command == "folders":
        await adapter.send_text(user_id, await admin.folders_overview())
        return
    if command == "purge":
        if argument:
            result = await admin.set_purge_hours(argument)
            await adapter.send_text(user_id, result.message)
            return
        await adapter.send_text(user_id, await admin.purge_overview())
        return
    if command == "invite":
        role = Role.ADMIN if argument.lower() in ("admin", "админ") else Role.MEMBER
        _, message = await admin.make_invite(person.id, role, None)
        await adapter.send_text(user_id, message)
        return
    if command == "mode":
        if argument in ("per_person", "shared", "per_group"):
            await ctx.settings.set("folder_mode", argument)
            await adapter.send_text(user_id, await admin.folders_overview())
            return
        await adapter.send_text(
            user_id, "Использование: /mode per_person | shared | per_group"
        )
        return
    if command == "notify":
        arg = argument.lower().strip()
        if arg in ("on", "вкл", "off", "выкл"):
            await ctx.settings.set("notify_admin_on_upload", arg in ("on", "вкл"))
        enabled = (await ctx.settings.get()).notify_admin_on_upload
        await adapter.send_text(
            user_id,
            f"Уведомления о загрузках: {'включены' if enabled else 'выключены'}.\n"
            "Переключить: /notify on | off",
        )
        return

    await adapter.send_text(user_id, _admin_help())


def _admin_help() -> str:
    return (
        "Команды администратора:\n"
        "/status — очередь, объём, ошибки\n"
        "/members — участники\n"
        "/invite [admin] — код приглашения\n"
        "/groups [название] — список групп или создать\n"
        "/storage — подключённые облака\n"
        "/folders — раскладка папок\n"
        "/mode per_person|shared|per_group — режим раскладки\n"
        "/purge [часы] — очистка чата\n"
        "/notify on|off — уведомлять о загрузках участников\n\n"
        "Полная панель с кнопками доступна в Telegram: /admin\n"
        "Виберу удаление сообщений ботом недоступно — это ограничение его API."
    )


def _extract(event: dict[str, Any], message: dict[str, Any]) -> IncomingMedia | None:
    kind = message.get("type")
    if kind not in ("file", "picture", "video"):
        return None

    media_url = message.get("media")
    if not media_url:
        return None

    token = str(event.get("message_token") or "")
    timestamp = event.get("timestamp")
    sent_at = (
        datetime.fromtimestamp(timestamp / 1000) if isinstance(timestamp, int | float) else None
    )
    sender_id = str((event.get("sender") or {}).get("id") or "")

    if kind == "file":
        source, name = SourceKind.FILE, message.get("file_name") or f"file_{token}"
    elif kind == "picture":
        source, name = SourceKind.PICTURE, message.get("file_name") or f"photo_{token}.jpg"
    else:
        source, name = SourceKind.VIDEO, message.get("file_name") or f"video_{token}.mp4"

    return IncomingMedia(
        platform=Platform.VIBER,
        chat_id=sender_id,
        message_id=token,
        source_kind=source,
        file_name=name,
        file_ref=media_url,
        size=message.get("size"),
        caption=message.get("text"),
        sent_at=sent_at,
    )
