"""The in-chat admin panel: inline keyboards over :class:`AdminService`."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ...context import AppContext
from ...core.models import FolderMode, Platform, Role
from ...i18n import t
from ...services.admin import MODE_LABELS, POLICY_LABELS, AdminService
from ...storage.registry import backend_choices, get_backend_class

log = logging.getLogger(__name__)

router = Router(name="admin")


class Ask(StatesGroup):
    """Free-text prompts. ``data['back']`` remembers which screen to return to."""

    root_folder = State()
    dir_template = State()
    file_template = State()
    purge_hours = State()
    group_name = State()
    person_folder = State()
    rclone_remote = State()
    local_path = State()


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


BACK = [btn("‹ Назад", "adm:menu")]


def service(ctx: AppContext) -> AdminService:
    return AdminService(ctx.repo, ctx.settings, ctx.members, ctx.storage)


async def is_admin(ctx: AppContext, user_id: int | str) -> bool:
    known = await ctx.members.resolve(Platform.TELEGRAM, str(user_id))
    return bool(known and known[0].is_admin and known[0].is_active)


async def guard(event: Message | CallbackQuery, ctx: AppContext) -> bool:
    user = event.from_user
    if user and await is_admin(ctx, user.id):
        return True
    lang = (await ctx.settings.get()).language
    text = t(lang, "access.admin_only")
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


async def show(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Edit in place when we can — the panel should not spam the chat."""
    if isinstance(event, CallbackQuery) and event.message:
        try:
            await event.message.edit_text(text, reply_markup=markup)
            await event.answer()
            return
        except Exception:  # noqa: BLE001 - identical text, or message too old to edit
            await event.answer()
            await event.message.answer(text, reply_markup=markup)
            return
    await event.answer(text, reply_markup=markup)


# ------------------------------------------------------------------- main menu

MENU_TEXT = (
    "⚙️ <b>Панель администратора</b>\n\n"
    "Здесь настраивается всё: куда складывать снимки, кто может их присылать "
    "и когда они исчезают из чата."
)

MENU_KB = kb(
    [btn("☁️ Хранилище", "adm:storage")],
    [btn("👨‍👩‍👧 Семья", "adm:family"), btn("🗂 Группы", "adm:groups")],
    [btn("📁 Раскладка папок", "adm:folders")],
    [btn("🧹 Очистка чата", "adm:purge"), btn("🎚 Качество", "adm:quality")],
    [btn("📊 Статус", "adm:status")],
)


@router.message(Command("admin"))
async def cmd_admin(message: Message, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(message, ctx):
        return
    await state.clear()
    await show(message, MENU_TEXT, MENU_KB)


@router.callback_query(F.data == "adm:menu")
async def cb_menu(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    await state.clear()
    await show(query, MENU_TEXT, MENU_KB)


# --------------------------------------------------------------------- storage


@router.callback_query(F.data == "adm:storage")
async def cb_storage(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    rows = []
    for account in await ctx.repo.list_storage_accounts():
        mark = "⭐️ " if account["is_default"] else ""
        rows.append([btn(f"{mark}{account['label']}", f"adm:st:{account['id']}")])
    rows.append([btn("＋ Подключить облако", "adm:st:add")])
    rows.append(BACK)
    await show(query, await service(ctx).storage_overview(), kb(*rows))


@router.callback_query(F.data == "adm:st:add")
async def cb_storage_add(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    rows = [[btn(info.title, f"adm:st:new:{info.key}")] for info in backend_choices()]
    rows.append([btn("‹ Назад", "adm:storage")])
    text = "☁️ <b>Куда складывать снимки?</b>\n\n" + "\n\n".join(
        f"<b>{info.title}</b>\n{info.description}" for info in backend_choices()
    )
    await show(query, text, kb(*rows))


@router.callback_query(F.data.startswith("adm:st:new:"))
async def cb_storage_new(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    key = query.data.rsplit(":", 1)[1]
    info = get_backend_class(key).info
    account_id = await service(ctx).add_storage(key, info.title)

    if key == "rclone":
        await state.set_state(Ask.rclone_remote)
        await state.update_data(account_id=account_id)
        await show(
            query,
            "Введите имя remote из вашей конфигурации rclone — например "
            "<code>box</code> или <code>dropbox:Photos</code>.\n\n"
            "Настроить его нужно заранее командой <code>rclone config</code> "
            "на этом же сервере.",
            kb([btn("‹ Отмена", "adm:storage")]),
        )
        return
    if key == "local":
        await state.set_state(Ask.local_path)
        await state.update_data(account_id=account_id)
        await show(
            query,
            "Введите путь к каталогу на диске, например <code>/mnt/nas/photos</code>.",
            kb([btn("‹ Отмена", "adm:storage")]),
        )
        return

    await cb_storage_card(query, ctx, account_id=account_id)


@router.callback_query(F.data.regexp(r"^adm:st:\d+$"))
async def cb_storage_card_cb(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    await cb_storage_card(query, ctx, int(query.data.rsplit(":", 1)[1]))


async def cb_storage_card(query: CallbackQuery, ctx: AppContext, account_id: int) -> None:
    record = await ctx.repo.get_storage_account(account_id)
    if not record:
        await show(query, "Хранилище не найдено.", kb([btn("‹ Назад", "adm:storage")]))
        return

    info = get_backend_class(record["backend"]).info
    linked = bool(record["credentials_enc"]) or not info.needs_oauth
    text = (
        f"☁️ <b>{record['label']}</b>\n\n"
        f"Тип: {info.title}\n"
        f"Корневая папка: <code>{record['root_folder']}</code>\n"
        f"Статус: {'подключено' if linked else '⚠️ требуется авторизация'}\n"
        f"{'⭐️ Используется по умолчанию' if record['is_default'] else ''}"
    )
    rows = []
    if info.needs_oauth:
        label = "🔗 Переподключить" if linked else "🔗 Подключить аккаунт"
        rows.append([btn(label, f"adm:st:link:{account_id}")])
    rows.append([btn("🧪 Проверить связь", f"adm:st:test:{account_id}")])
    rows.append([btn("📁 Корневая папка", f"adm:st:root:{account_id}")])
    if not record["is_default"]:
        rows.append([btn("⭐️ Сделать основным", f"adm:st:def:{account_id}")])
    rows.append([btn("🗑 Удалить", f"adm:st:del:{account_id}")])
    rows.append([btn("‹ Назад", "adm:storage")])
    await show(query, text, kb(*rows))


@router.callback_query(F.data.startswith("adm:st:link:"))
async def cb_storage_link(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    account_id = int(query.data.rsplit(":", 1)[1])
    record = await ctx.repo.get_storage_account(account_id)
    if not record:
        await query.answer("Хранилище не найдено", show_alert=True)
        return

    redirect_uri = ctx.config.oauth_redirect_uri
    if not redirect_uri:
        await show(
            query,
            "⚠️ Для подключения по OAuth нужен публичный HTTPS-адрес.\n\n"
            "Задайте <code>FOTOHU_PUBLIC_URL</code> в <code>.env</code> и перезапустите "
            "бот. В docker-compose для этого есть профиль <code>tls</code> с Caddy, "
            "который сам выпустит сертификат.\n\n"
            "Если публичного адреса нет — используйте вариант «Другое облако (rclone)»: "
            "там авторизация делается командой <code>rclone config</code> на сервере.",
            kb([btn("‹ Назад", f"adm:st:{account_id}")]),
        )
        return

    from ...core.crypto import new_state_token

    known = await ctx.members.resolve(Platform.TELEGRAM, str(query.from_user.id))
    person_id = known[0].id if known else None
    try:
        start = ctx.storage.begin_auth(record["backend"], redirect_uri)
    except Exception as exc:  # noqa: BLE001
        await show(query, f"⚠️ {exc}", kb([btn("‹ Назад", f"adm:st:{account_id}")]))
        return

    state_token = new_state_token()
    await ctx.repo.create_oauth_state(
        state=state_token,
        backend=record["backend"],
        person_id=person_id,
        verifier=start.verifier,
        payload={"account_id": account_id},
    )
    separator = "&" if "?" in start.url else "?"
    url = f"{start.url}{separator}state={state_token}"
    await show(
        query,
        "🔗 <b>Подключение аккаунта</b>\n\n"
        "Откройте ссылку и разрешите доступ. Ссылка одноразовая и живёт 10 минут.\n\n"
        f'<a href="{url}">Открыть страницу входа</a>',
        kb([btn("‹ Назад", f"adm:st:{account_id}")]),
    )


@router.callback_query(F.data.startswith("adm:st:test:"))
async def cb_storage_test(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    account_id = int(query.data.rsplit(":", 1)[1])
    await query.answer("Проверяю…")
    result = await service(ctx).test_storage(account_id)
    await show(query, result.message, kb([btn("‹ Назад", f"adm:st:{account_id}")]))


@router.callback_query(F.data.startswith("adm:st:def:"))
async def cb_storage_default(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    account_id = int(query.data.rsplit(":", 1)[1])
    await ctx.repo.set_default_storage(account_id)
    await query.answer("Теперь это основное хранилище")
    if ctx.uploader:
        ctx.uploader.notify()  # anything queued for want of storage can go now
    await cb_storage_card(query, ctx, account_id)


@router.callback_query(F.data.startswith("adm:st:del:"))
async def cb_storage_delete(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    account_id = int(query.data.rsplit(":", 1)[1])
    await ctx.repo.delete_storage_account(account_id)
    await query.answer("Удалено")
    await cb_storage(query, ctx)


@router.callback_query(F.data.startswith("adm:st:root:"))
async def cb_storage_root(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    account_id = int(query.data.rsplit(":", 1)[1])
    await state.set_state(Ask.root_folder)
    await state.update_data(account_id=account_id)
    await show(
        query,
        "Введите название корневой папки в облаке, например <code>FotoHu</code> "
        "или <code>Семейный архив</code>.",
        kb([btn("‹ Отмена", f"adm:st:{account_id}")]),
    )


# ---------------------------------------------------------------------- family


@router.callback_query(F.data == "adm:family")
async def cb_family(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    rows = [
        [btn(f"{'👑' if p.is_admin else '👤'} {p.name}", f"adm:p:{p.id}")]
        for p in await ctx.repo.list_people()
    ]
    rows.append([btn("🎟 Создать приглашение", "adm:inv")])
    rows.append(
        [btn(f"{'🔔' if settings.notify_admin_on_upload else '🔕'} Уведомлять о загрузках",
             "adm:fam:notify")]
    )
    rows.append(BACK)
    await show(query, await service(ctx).family_overview(), kb(*rows))


@router.callback_query(F.data == "adm:fam:notify")
async def cb_family_notify(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    await ctx.settings.set("notify_admin_on_upload", not settings.notify_admin_on_upload)
    await query.answer("Готово")
    await cb_family(query, ctx)


@router.callback_query(F.data.regexp(r"^adm:p:\d+$"))
async def cb_person(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    person_id = int(query.data.rsplit(":", 1)[1])
    await _render_person(query, ctx, person_id)


async def _render_person(query: CallbackQuery, ctx: AppContext, person_id: int) -> None:
    person = await ctx.repo.get_person(person_id)
    if not person:
        await show(query, "Участник не найден.", kb([btn("‹ Назад", "adm:family")]))
        return
    rows = [
        [btn("📁 Раскладка", f"adm:p:{person_id}:mode")],
        [btn("🗂 Группа", f"adm:p:{person_id}:grp")],
        [btn("✏️ Имя папки", f"adm:p:{person_id}:fld")],
        [
            btn("👑 Роль", f"adm:p:{person_id}:role"),
            btn("🚫 Разблокировать" if not person.is_active else "🚫 Заблокировать",
                f"adm:p:{person_id}:blk"),
        ],
        [btn("‹ Назад", "adm:family")],
    ]
    await show(query, await service(ctx).person_card(person), kb(*rows))


@router.callback_query(F.data.regexp(r"^adm:p:\d+:(role|blk)$"))
async def cb_person_toggle(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    _, _, person_id_raw, action = query.data.split(":")
    person = await ctx.repo.get_person(int(person_id_raw))
    if not person:
        await query.answer("Не найдено", show_alert=True)
        return
    svc = service(ctx)
    result = await (svc.toggle_role(person) if action == "role" else svc.toggle_block(person))
    await query.answer(result.message, show_alert=not result.ok)
    await _render_person(query, ctx, person.id)


@router.callback_query(F.data.regexp(r"^adm:p:\d+:mode$"))
async def cb_person_mode(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    person_id = int(query.data.split(":")[2])
    rows = [
        [btn(f"{label}", f"adm:p:{person_id}:mode:{mode}")]
        for mode, label in MODE_LABELS.items()
    ]
    rows.append([btn("↩️ Как у всех (снять персональную)", f"adm:p:{person_id}:mode:none")])
    rows.append([btn("‹ Назад", f"adm:p:{person_id}")])
    await show(
        query,
        "📁 <b>Раскладка для этого участника</b>\n\n"
        "Персональная настройка перекрывает общую. Так можно, например, оставить "
        "всем свои папки, а двоих отправить в одну общую.",
        kb(*rows),
    )


@router.callback_query(F.data.regexp(r"^adm:p:\d+:mode:\w+$"))
async def cb_person_mode_set(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    parts = query.data.split(":")
    person_id, value = int(parts[2]), parts[4]
    person = await ctx.repo.get_person(person_id)
    if not person:
        await query.answer("Не найдено", show_alert=True)
        return
    mode = None if value == "none" else FolderMode(value)
    result = await service(ctx).set_person_folder_mode(person, mode)
    await query.answer(result.message)
    await _render_person(query, ctx, person_id)


@router.callback_query(F.data.regexp(r"^adm:p:\d+:grp$"))
async def cb_person_group(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    person_id = int(query.data.split(":")[2])
    rows = [
        [btn(g.name, f"adm:p:{person_id}:grp:{g.id}")] for g in await ctx.repo.list_groups()
    ]
    rows.append([btn("— Без группы", f"adm:p:{person_id}:grp:0")])
    rows.append([btn("‹ Назад", f"adm:p:{person_id}")])
    await show(query, "🗂 Выберите группу:", kb(*rows))


@router.callback_query(F.data.regexp(r"^adm:p:\d+:grp:\d+$"))
async def cb_person_group_set(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    parts = query.data.split(":")
    person_id, group_id = int(parts[2]), int(parts[4])
    person = await ctx.repo.get_person(person_id)
    if not person:
        await query.answer("Не найдено", show_alert=True)
        return
    result = await service(ctx).set_person_group(person, group_id or None)
    await query.answer(result.message.split("\n")[0])
    await _render_person(query, ctx, person_id)


@router.callback_query(F.data.regexp(r"^adm:p:\d+:fld$"))
async def cb_person_folder(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    person_id = int(query.data.split(":")[2])
    await state.set_state(Ask.person_folder)
    await state.update_data(person_id=person_id)
    await show(
        query,
        "Введите название личной папки этого участника (латиницей, без пробелов), "
        "например <code>dad</code>.",
        kb([btn("‹ Отмена", f"adm:p:{person_id}")]),
    )


# --------------------------------------------------------------------- invites


@router.callback_query(F.data == "adm:inv")
async def cb_invite(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    rows = [[btn("👤 Участник", "adm:inv:make:member:0")]]
    for group in await ctx.repo.list_groups():
        rows.append([btn(f"👤 Участник → «{group.name}»", f"adm:inv:make:member:{group.id}")])
    rows.append([btn("👑 Администратор", "adm:inv:make:admin:0")])
    rows.append([btn("‹ Назад", "adm:family")])
    await show(
        query,
        "🎟 <b>Новое приглашение</b>\n\n"
        "Код действует 72 часа и рассчитан на одно применение.",
        kb(*rows),
    )


@router.callback_query(F.data.startswith("adm:inv:make:"))
async def cb_invite_make(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    _, _, _, role_raw, group_raw = query.data.split(":")
    known = await ctx.members.resolve(Platform.TELEGRAM, str(query.from_user.id))
    _, text = await service(ctx).make_invite(
        admin_id=known[0].id if known else None,
        role=Role(role_raw),
        group_id=int(group_raw) or None,
    )
    await show(query, text, kb([btn("‹ Назад", "adm:family")]))


# ---------------------------------------------------------------------- groups


@router.callback_query(F.data == "adm:groups")
async def cb_groups(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    rows = [
        [btn(f"🗑 {g.name}", f"adm:grp:del:{g.id}")] for g in await ctx.repo.list_groups()
    ]
    rows.append([btn("＋ Новая группа", "adm:grp:new")])
    rows.append(BACK)
    await show(query, await service(ctx).groups_overview(), kb(*rows))


@router.callback_query(F.data == "adm:grp:new")
async def cb_group_new(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    await state.set_state(Ask.group_name)
    await show(
        query,
        "Введите название группы, например <code>Родители</code>.\n\n"
        "Потом назначьте в неё участников и выберите режим «по группам».",
        kb([btn("‹ Отмена", "adm:groups")]),
    )


@router.callback_query(F.data.startswith("adm:grp:del:"))
async def cb_group_delete(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    await ctx.repo.delete_group(int(query.data.rsplit(":", 1)[1]))
    await query.answer("Группа удалена")
    await cb_groups(query, ctx)


# --------------------------------------------------------------------- folders


@router.callback_query(F.data == "adm:folders")
async def cb_folders(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    rows = [
        [btn(("● " if settings.folder_mode == mode else "○ ") + label, f"adm:fld:mode:{mode}")]
        for mode, label in MODE_LABELS.items()
    ]
    rows.append([btn("✏️ Шаблон папки", "adm:fld:dir")])
    rows.append([btn("✏️ Шаблон имени файла", "adm:fld:file")])
    rows.append(
        [btn(f"📅 Дата: {'EXIF' if settings.prefer_exif_date else 'сообщения'}", "adm:fld:exif")]
    )
    rows.append(BACK)
    await show(query, await service(ctx).folders_overview(), kb(*rows))


@router.callback_query(F.data.startswith("adm:fld:mode:"))
async def cb_folder_mode(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    await ctx.settings.set("folder_mode", query.data.rsplit(":", 1)[1])
    await query.answer("Готово")
    await cb_folders(query, ctx)


@router.callback_query(F.data == "adm:fld:exif")
async def cb_folder_exif(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    await ctx.settings.set("prefer_exif_date", not settings.prefer_exif_date)
    await query.answer("Готово")
    await cb_folders(query, ctx)


@router.callback_query(F.data.in_({"adm:fld:dir", "adm:fld:file"}))
async def cb_folder_template(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    is_dir = query.data.endswith("dir")
    await state.set_state(Ask.dir_template if is_dir else Ask.file_template)
    example = (
        "<code>{root}/{owner}/{yyyy}/{yyyy-mm}</code>"
        if is_dir
        else "<code>{yyyy-mm-dd}_{hhmmss}_{filename}</code>"
    )
    await show(
        query,
        f"Введите шаблон. Например:\n{example}\n\n"
        "Подстановки: <code>{root} {owner} {person} {group} {yyyy} {mm} {dd} "
        "{yyyy-mm} {yyyy-mm-dd} {hhmmss} {filename} {stem} {ext} {quality}</code>",
        kb([btn("‹ Отмена", "adm:folders")]),
    )


# ------------------------------------------------------------------ cleanup/QA


@router.callback_query(F.data == "adm:purge")
async def cb_purge(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    rows = [
        [btn(f"{'✅' if settings.purge_enabled else '⬜️'} Включена", "adm:pg:toggle")],
        [btn(f"⏱ Через {settings.purge_after_hours} ч", "adm:pg:hours")],
        [btn(f"{'✅' if settings.purge_bot_replies else '⬜️'} Удалять и ответы бота",
             "adm:pg:replies")],
        BACK,
    ]
    await show(query, await service(ctx).purge_overview(), kb(*rows))


@router.callback_query(F.data.in_({"adm:pg:toggle", "adm:pg:replies"}))
async def cb_purge_toggle(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    if query.data.endswith("toggle"):
        await ctx.settings.set("purge_enabled", not settings.purge_enabled)
    else:
        await ctx.settings.set("purge_bot_replies", not settings.purge_bot_replies)
    await query.answer("Готово")
    await cb_purge(query, ctx)


@router.callback_query(F.data == "adm:pg:hours")
async def cb_purge_hours(query: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    if not await guard(query, ctx):
        return
    await state.set_state(Ask.purge_hours)
    await show(
        query,
        "Через сколько часов после успешной загрузки удалять снимок из чата?\n\n"
        "<code>0</code> — сразу\n"
        "<code>1</code> — через час (по умолчанию)\n"
        "<code>24</code> — через сутки\n\n"
        "⚠️ Telegram не удаляет сообщения старше <b>48 часов</b>, поэтому значения "
        "от 48 и выше работать не будут — это ограничение самого Telegram.",
        kb([btn("‹ Отмена", "adm:purge")]),
    )


@router.callback_query(F.data == "adm:quality")
async def cb_quality(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    rows = [
        [btn(("● " if settings.photo_policy == policy else "○ ") + label,
             f"adm:q:pol:{policy}")]
        for policy, label in POLICY_LABELS.items()
    ]
    rows.append([btn(f"{'✅' if settings.verify_hashes else '⬜️'} Сверять контрольные суммы",
                     "adm:q:verify")])
    rows.append([btn(f"{'✅' if settings.dedupe_enabled else '⬜️'} Пропускать дубликаты",
                     "adm:q:dedupe")])
    rows.append(BACK)
    await show(query, await service(ctx).quality_overview(), kb(*rows))


@router.callback_query(F.data.startswith("adm:q:pol:"))
async def cb_quality_policy(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    await ctx.settings.set("photo_policy", query.data.rsplit(":", 1)[1])
    await query.answer("Готово")
    await cb_quality(query, ctx)


@router.callback_query(F.data.in_({"adm:q:verify", "adm:q:dedupe"}))
async def cb_quality_toggle(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    settings = await ctx.settings.get()
    if query.data.endswith("verify"):
        await ctx.settings.set("verify_hashes", not settings.verify_hashes)
    else:
        await ctx.settings.set("dedupe_enabled", not settings.dedupe_enabled)
    await query.answer("Готово")
    await cb_quality(query, ctx)


# ---------------------------------------------------------------------- status


@router.callback_query(F.data == "adm:status")
async def cb_status(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    rows = [
        [btn("🔄 Повторить неудачные", "adm:status:retry")],
        [btn("↻ Обновить", "adm:status")],
        BACK,
    ]
    await show(query, await service(ctx).status(), kb(*rows))


@router.callback_query(F.data == "adm:status:retry")
async def cb_status_retry(query: CallbackQuery, ctx: AppContext) -> None:
    if not await guard(query, ctx):
        return
    result = await service(ctx).retry_failed()
    if ctx.uploader:
        ctx.uploader.notify()
    await query.answer(result.message)
    await cb_status(query, ctx)


# ------------------------------------------------------------- free-text answers


@router.message(Ask.root_folder)
async def on_root_folder(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    result = await service(ctx).set_root_folder(data["account_id"], message.text or "")
    await state.clear()
    await message.answer(result.message, reply_markup=MENU_KB)


@router.message(Ask.dir_template)
async def on_dir_template(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await ctx.settings.set("dir_template", (message.text or "").strip())
    await state.clear()
    await message.answer(await service(ctx).folders_overview(), reply_markup=MENU_KB)


@router.message(Ask.file_template)
async def on_file_template(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await ctx.settings.set("file_template", (message.text or "").strip())
    await state.clear()
    await message.answer(await service(ctx).folders_overview(), reply_markup=MENU_KB)


@router.message(Ask.purge_hours)
async def on_purge_hours(message: Message, ctx: AppContext, state: FSMContext) -> None:
    result = await service(ctx).set_purge_hours(message.text or "")
    if not result.ok:
        await message.answer(result.message)
        return
    await state.clear()
    await message.answer(result.message, reply_markup=MENU_KB)


@router.message(Ask.group_name)
async def on_group_name(message: Message, ctx: AppContext, state: FSMContext) -> None:
    result = await service(ctx).create_group(message.text or "")
    if not result.ok:
        await message.answer(result.message)
        return
    await state.clear()
    await message.answer(result.message, reply_markup=MENU_KB)


@router.message(Ask.person_folder)
async def on_person_folder(message: Message, ctx: AppContext, state: FSMContext) -> None:
    from ...core.naming import slugify

    data = await state.get_data()
    folder = slugify(message.text or "")
    await ctx.repo.update_person(data["person_id"], personal_folder=folder)
    await state.clear()
    await message.answer(f"Папка участника: <code>{folder}</code>", reply_markup=MENU_KB)


@router.message(Ask.rclone_remote)
async def on_rclone_remote(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    remote = (message.text or "").strip()
    svc = service(ctx)
    await svc.set_storage_extra(
        data["account_id"],
        remote=remote,
        binary=ctx.config.rclone_binary,
        config_path=ctx.config.rclone_config,
    )
    await state.clear()
    result = await svc.test_storage(data["account_id"])
    await message.answer(result.message, reply_markup=MENU_KB)


@router.message(Ask.local_path)
async def on_local_path(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    svc = service(ctx)
    await svc.set_storage_extra(data["account_id"], base_path=(message.text or "").strip())
    await state.clear()
    result = await svc.test_storage(data["account_id"])
    await message.answer(result.message, reply_markup=MENU_KB)


__all__ = ["router"]
