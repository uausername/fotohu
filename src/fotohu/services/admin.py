"""Admin operations, expressed without reference to any messenger.

Telegram renders these as inline-keyboard screens and Viber as text commands, but
both go through this one service so the two interfaces cannot drift apart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..core.errors import StorageError
from ..core.models import FolderMode, Group, Person, PhotoPolicy, Role
from ..core.naming import slugify
from ..db.repo import Repo
from ..storage.registry import StorageRegistry, backend_choices
from .members import MemberService
from .settings import TELEGRAM_DELETE_WINDOW_HOURS, SettingsService

log = logging.getLogger(__name__)


def human_size(num: int | None) -> str:
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str


MODE_LABELS = {
    FolderMode.PER_PERSON: "у каждого своя папка",
    FolderMode.SHARED: "все в одну общую",
    FolderMode.PER_GROUP: "по группам",
}

POLICY_LABELS = {
    PhotoPolicy.REJECT: "отклонять и объяснять",
    PhotoPolicy.SAVE_MARKED: "сохранять в _compressed",
    PhotoPolicy.SAVE: "сохранять как обычные",
}


class AdminService:
    def __init__(
        self,
        repo: Repo,
        settings: SettingsService,
        members: MemberService,
        storage: StorageRegistry,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.members = members
        self.storage = storage

    # ------------------------------------------------------------------ storage

    async def storage_overview(self) -> str:
        accounts = await self.repo.list_storage_accounts()
        if not accounts:
            lines = ["☁️ <b>Хранилище</b>\n", "Пока не подключено ни одного облака.\n"]
            lines.append("Доступные варианты:")
            for info in backend_choices():
                lines.append(f"• <b>{info.title}</b> — {info.description}")
            return "\n".join(lines)

        lines = ["☁️ <b>Хранилище</b>\n"]
        for account in accounts:
            mark = "⭐️" if account["is_default"] else "  "
            linked = "подключено" if account["credentials_enc"] else "⚠️ не авторизовано"
            lines.append(
                f"{mark} <b>{account['label']}</b> ({account['backend']})\n"
                f"     корень: <code>{account['root_folder']}</code> — {linked}"
            )
        return "\n".join(lines)

    async def test_storage(self, account_id: int) -> ActionResult:
        record = await self.repo.get_storage_account(account_id)
        if not record:
            return ActionResult(False, "Такого хранилища нет.")
        if not record["credentials_enc"] and record["backend"] not in ("local", "rclone"):
            return ActionResult(False, "Аккаунт ещё не авторизован — нажмите «Подключить».")

        backend = await self.storage.build(record)
        try:
            detail = await backend.check()
            quota = await backend.quota()
            text = f"✅ Связь есть: {detail}"
            if quota and quota.total:
                text += (
                    f"\n💾 Занято {human_size(quota.used)} из {human_size(quota.total)}"
                    f" (свободно {human_size(quota.free)})"
                )
            return ActionResult(True, text)
        except StorageError as exc:
            return ActionResult(False, f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("storage check failed")
            return ActionResult(False, f"❌ {type(exc).__name__}: {exc}")
        finally:
            await self.storage.persist_credentials(backend)
            await backend.close()

    async def add_storage(self, backend_key: str, label: str | None = None) -> int:
        settings = await self.settings.get()
        account_id = await self.repo.create_storage_account(
            backend=backend_key,
            label=label or backend_key,
            root_folder=settings.root_folder,
        )
        # First one configured becomes the default so uploads start working.
        if len(await self.repo.list_storage_accounts()) == 1:
            await self.repo.set_default_storage(account_id)
        return account_id

    async def set_storage_extra(self, account_id: int, **values: Any) -> None:
        record = await self.repo.get_storage_account(account_id)
        extra = json.loads((record or {}).get("extra_json") or "{}")
        extra.update(values)
        await self.repo.update_storage_account(account_id, extra_json=json.dumps(extra))

    async def set_root_folder(self, account_id: int, folder: str) -> ActionResult:
        clean = folder.strip().strip("/")
        if not clean:
            return ActionResult(False, "Пустое имя папки.")
        await self.repo.update_storage_account(account_id, root_folder=clean)
        # Folder ids were resolved against the old root and are now meaningless.
        await self.repo.clear_folder_cache(account_id)
        return ActionResult(True, f"Корневая папка: <code>{clean}</code>")

    # ------------------------------------------------------------------- family

    async def family_overview(self) -> str:
        people = await self.repo.list_people()
        if not people:
            return "👨‍👩‍👧 <b>Семья</b>\n\nПока никого нет. Создайте приглашение."

        stats = await self.repo.per_person_stats()
        groups = {g.id: g for g in await self.repo.list_groups()}
        settings = await self.settings.get()

        lines = ["👨‍👩‍👧 <b>Семья</b>\n"]
        for person in people:
            counters = stats.get(person.id, {"count": 0, "bytes": 0})
            badge = "👑" if person.is_admin else "👤"
            if not person.is_active:
                badge = "🚫"
            mode = person.folder_mode_override or settings.folder_mode
            where = MODE_LABELS.get(mode, str(mode))
            if mode == FolderMode.PER_GROUP and person.group_id in groups:
                where = f"группа «{groups[person.group_id].name}»"
            lines.append(
                f"{badge} <b>{person.name}</b> — {where}\n"
                f"     {counters['count']} шт., {human_size(counters['bytes'])}"
            )
        return "\n".join(lines)

    async def person_card(self, person: Person) -> str:
        settings = await self.settings.get()
        group = await self.repo.get_group(person.group_id)
        accounts = await self.repo.list_accounts(person.id)
        stats = (await self.repo.per_person_stats()).get(person.id, {"count": 0, "bytes": 0})
        mode = person.folder_mode_override or settings.folder_mode

        return "\n".join(
            [
                f"👤 <b>{person.name}</b>",
                f"Роль: {'администратор' if person.is_admin else 'участник'}",
                f"Статус: {'активен' if person.is_active else '🚫 заблокирован'}",
                f"Группа: {group.name if group else '—'}",
                f"Раскладка: {MODE_LABELS.get(mode, str(mode))}"
                + (" (персональная настройка)" if person.folder_mode_override else ""),
                f"Папка: <code>{await self.members.folder_preview(person)}</code>",
                f"Аккаунты: {', '.join(a.platform for a in accounts) or '—'}",
                f"Сохранено: {stats['count']} шт., {human_size(stats['bytes'])}",
            ]
        )

    async def toggle_block(self, person: Person) -> ActionResult:
        new_status = "blocked" if person.is_active else "active"
        await self.repo.update_person(person.id, status=new_status)
        return ActionResult(
            True, "Доступ приостановлен." if new_status == "blocked" else "Доступ восстановлен."
        )

    async def toggle_role(self, person: Person) -> ActionResult:
        if person.is_admin:
            admins = [p for p in await self.repo.list_people() if p.is_admin and p.is_active]
            if len(admins) <= 1:
                return ActionResult(False, "Это последний администратор — роль нельзя снять.")
            await self.repo.update_person(person.id, role=str(Role.MEMBER))
            return ActionResult(True, "Теперь это обычный участник.")
        await self.repo.update_person(person.id, role=str(Role.ADMIN))
        return ActionResult(True, "Назначен администратором.")

    async def set_person_folder_mode(
        self, person: Person, mode: FolderMode | None
    ) -> ActionResult:
        await self.repo.update_person(
            person.id, folder_mode_override=str(mode) if mode else None
        )
        if mode is None:
            return ActionResult(True, "Персональная настройка снята — действует общая.")
        return ActionResult(True, f"Раскладка для {person.name}: {MODE_LABELS[mode]}")

    async def set_person_group(self, person: Person, group_id: int | None) -> ActionResult:
        await self.repo.update_person(person.id, group_id=group_id)
        group = await self.repo.get_group(group_id)
        if group is None:
            return ActionResult(True, "Убран(а) из группы.")
        # Being in a group is meaningless unless the layout actually uses groups.
        settings = await self.settings.get()
        effective = person.folder_mode_override or settings.folder_mode
        note = ""
        if effective != FolderMode.PER_GROUP:
            note = (
                "\n\n⚠️ Сейчас раскладка не «по группам», поэтому общая папка не "
                "используется. Включите её глобально или лично для этого участника."
            )
        return ActionResult(True, f"Группа: «{group.name}».{note}")

    # ------------------------------------------------------------------- groups

    async def create_group(self, name: str) -> ActionResult:
        clean = name.strip()
        if not clean:
            return ActionResult(False, "Пустое название.")
        if await self.repo.get_group_by_name(clean):
            return ActionResult(False, "Группа с таким названием уже есть.")
        group = await self.repo.create_group(clean, slugify(clean))
        return ActionResult(
            True, f"Группа «{group.name}» создана (папка <code>{group.folder}</code>)."
        )

    async def groups_overview(self) -> str:
        groups = await self.repo.list_groups()
        if not groups:
            return (
                "🗂 <b>Группы</b>\n\nГрупп пока нет.\n\n"
                "Группа — это общая папка для нескольких членов семьи: например, "
                "родители складывают в одну, а дети — каждый в свою."
            )
        people = await self.repo.list_people()
        lines = ["🗂 <b>Группы</b>\n"]
        for group in groups:
            members = [p.name for p in people if p.group_id == group.id]
            lines.append(
                f"• <b>{group.name}</b> → <code>{group.folder}</code>\n"
                f"     {', '.join(members) if members else 'пока никого'}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ invites

    async def make_invite(
        self, admin_id: int | None, role: Role, group_id: int | None, max_uses: int = 1
    ) -> tuple[str, str]:
        invite = await self.members.create_invite(
            created_by=admin_id, role=role, group_id=group_id, max_uses=max_uses
        )
        group: Group | None = await self.repo.get_group(group_id)
        text = (
            f"🎟 Код приглашения: <code>{invite['code']}</code>\n\n"
            f"Роль: {'администратор' if role == Role.ADMIN else 'участник'}\n"
            f"Группа: {group.name if group else '—'}\n"
            f"Использований: {invite['max_uses']}\n"
            f"Действует до: {invite['expires_at'] or 'бессрочно'}\n\n"
            "Перешлите родственнику — он отправит боту:\n"
            f"<code>/join {invite['code']}</code>"
        )
        return invite["code"], text

    # ------------------------------------------------------------------ cleanup

    async def purge_overview(self) -> str:
        settings = await self.settings.get()
        stats = await self.repo.stats()
        state = "включена" if settings.purge_enabled else "выключена"

        lines = [
            "🧹 <b>Очистка мессенджера</b>\n",
            f"Состояние: <b>{state}</b>",
            f"Удалять через: <b>{settings.purge_after_hours} ч</b> после успешной загрузки",
            f"Удалять и ответы бота: {'да' if settings.purge_bot_replies else 'нет'}",
            "",
            "ℹ️ Снимок удаляется <b>только</b> после того, как копия проверена в облаке.",
        ]
        if settings.purge_exceeds_telegram_window:
            lines.append(
                f"\n⚠️ <b>Telegram не удаляет сообщения старше "
                f"{TELEGRAM_DELETE_WINDOW_HOURS} ч.</b> При текущем значении "
                f"({settings.purge_after_hours} ч) удаление будет отклоняться. "
                "Поставьте меньше 48."
            )
        lines.append("\n⚠️ В Viber удаление сообщений ботом не поддерживается вовсе — "
                     "это ограничение их API, а не бота.")
        if stats["purge_failed"]:
            lines.append(f"\n❌ Не удалось удалить: {stats['purge_failed']} шт. (см. Статус)")
        return "\n".join(lines)

    async def set_purge_hours(self, raw: str) -> ActionResult:
        try:
            hours = int(raw.strip())
        except ValueError:
            return ActionResult(False, "Нужно целое число часов, например 1 или 24.")
        if hours < 0:
            return ActionResult(False, "Отрицательное значение не имеет смысла.")

        await self.settings.set("purge_after_hours", hours)
        if hours >= TELEGRAM_DELETE_WINDOW_HOURS:
            return ActionResult(
                True,
                f"Установлено {hours} ч, но учтите: Telegram отказывается удалять "
                f"сообщения старше {TELEGRAM_DELETE_WINDOW_HOURS} ч, поэтому такие "
                "снимки останутся в чате и попадут в список ошибок.",
            )
        if hours == 0:
            return ActionResult(True, "Удаление сразу после успешной загрузки.")
        return ActionResult(True, f"Удаление через {hours} ч после загрузки.")

    # ------------------------------------------------------------------- status

    async def status(self) -> str:
        stats = await self.repo.stats()
        settings = await self.settings.get()
        storage = await self.repo.get_default_storage()

        by_state = stats["by_state"]
        queued = by_state.get("pending", {}).get("count", 0) + by_state.get(
            "uploading", {}
        ).get("count", 0)

        lines = [
            "📊 <b>Статус</b>\n",
            f"⏳ В очереди: {queued}",
            f"✅ Сохранено всего: {by_state.get('done', {}).get('count', 0)} "
            f"({human_size(by_state.get('done', {}).get('bytes', 0))})",
            f"📅 За этот месяц: {stats['month']['n']} ({human_size(stats['month']['bytes'])})",
            f"♻️ Дубликатов пропущено: {by_state.get('skipped_dup', {}).get('count', 0)}",
            f"⚠️ Отклонено сжатых: {by_state.get('rejected', {}).get('count', 0)}",
            f"❌ Ошибок: {by_state.get('failed', {}).get('count', 0)}",
        ]

        if storage:
            backend = await self.storage.build(storage)
            try:
                quota = await backend.quota()
                if quota and quota.total:
                    lines.append(
                        f"\n💾 {storage['label']}: занято {human_size(quota.used)} "
                        f"из {human_size(quota.total)}"
                    )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"\n⚠️ Не удалось спросить квоту: {exc}")
            finally:
                await self.storage.persist_credentials(backend)
                await backend.close()
        else:
            lines.append("\n⚠️ Облако не подключено — загрузки стоят в очереди.")

        if stats["purge_failed"]:
            lines.append(
                f"\n🧹 Не удалось удалить из чата: {stats['purge_failed']} шт."
                f"\n     Обычная причина — прошло больше {TELEGRAM_DELETE_WINDOW_HOURS} ч "
                f"(сейчас настроено {settings.purge_after_hours} ч)."
            )

        if stats["recent_errors"]:
            lines.append("\n<b>Последние ошибки</b>")
            for error in stats["recent_errors"]:
                lines.append(f"• {error['file_name']}: {error['last_error'][:120]}")
        return "\n".join(lines)

    async def retry_failed(self) -> ActionResult:
        count = await self.repo.reset_failed()
        return ActionResult(True, f"Поставлено в очередь заново: {count} шт.")

    # ------------------------------------------------------------------ folders

    async def folders_overview(self) -> str:
        settings = await self.settings.get()
        return "\n".join(
            [
                "🗂 <b>Раскладка папок</b>\n",
                f"Режим: <b>{MODE_LABELS[settings.folder_mode]}</b>",
                f"Шаблон папки: <code>{settings.dir_template}</code>",
                f"Шаблон имени файла: <code>{settings.file_template}</code>",
                "Дата берётся из: "
                + ("EXIF снимка" if settings.prefer_exif_date else "даты сообщения"),
                "",
                "Доступные подстановки:",
                "<code>{root} {owner} {person} {group} {yyyy} {mm} {dd} {yyyy-mm}</code>",
                "<code>{filename} {stem} {ext} {hhmmss} {quality}</code>",
            ]
        )

    async def quality_overview(self) -> str:
        settings = await self.settings.get()
        return "\n".join(
            [
                "🎚 <b>Качество</b>\n",
                f"Сжатые фото: <b>{POLICY_LABELS[settings.photo_policy]}</b>",
                f"Сверять контрольные суммы: {'да' if settings.verify_hashes else 'нет'}",
                f"Пропускать дубликаты: {'да' if settings.dedupe_enabled else 'нет'}",
                "",
                "ℹ️ Файлы, присланные <b>документом</b> (Telegram) или <b>файлом</b> (Viber), "
                "сохраняются побайтно, вместе с EXIF.",
                "Всё, что мессенджер пережал у себя на сервере, восстановить нельзя — "
                "поэтому по умолчанию такие снимки отклоняются с подсказкой.",
            ]
        )
