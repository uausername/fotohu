"""Telegram wiring: build the Bot/Dispatcher and run it (polling or webhook)."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from ...config import Config
from ...context import AppContext
from .adapter import TelegramAdapter
from .admin import router as admin_router
from .handlers import router as member_router

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="me", description="Моя папка и статистика"),
    BotCommand(command="last", description="Последние загрузки"),
    BotCommand(command="howto", description="Как слать фото без сжатия"),
    BotCommand(command="join", description="Войти по коду приглашения"),
    BotCommand(command="admin", description="Панель администратора"),
    BotCommand(command="help", description="Справка"),
]


def build_bot(config: Config) -> Bot:
    assert config.telegram.token
    session = None
    if config.telegram.api_base:
        # Pointing at a self-hosted telegram-bot-api lifts the 20 MB download cap.
        base = config.telegram.api_base.rstrip("/")
        session = AiohttpSession(
            api=TelegramAPIServer(
                base=f"{base}/bot{{token}}/{{method}}",
                file=f"{base}/file/bot{{token}}/{{path}}",
                is_local=config.telegram.local_mode,
            )
        )
        log.info("using custom Bot API server at %s", base)

    return Bot(
        token=config.telegram.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    dp = Dispatcher()
    # Every handler takes `ctx: AppContext`; aiogram injects it from here.
    dp["ctx"] = ctx
    dp.include_router(admin_router)
    dp.include_router(member_router)
    return dp


async def setup(ctx: AppContext) -> tuple[Bot, Dispatcher]:
    bot = build_bot(ctx.config)
    ctx.register(TelegramAdapter(bot, local_mode=ctx.config.telegram.local_mode))
    dp = build_dispatcher(ctx)
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as exc:  # noqa: BLE001 - cosmetic; never block startup on it
        log.warning("could not publish the command list: %s", exc)
    return bot, dp


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("telegram: long polling")
    await dp.start_polling(bot, handle_signals=False)


async def install_webhook(bot: Bot, config: Config) -> None:
    if not config.public_url:
        raise RuntimeError("TELEGRAM_USE_WEBHOOK needs FOTOHU_PUBLIC_URL to be set")
    url = config.public_url.rstrip("/") + config.telegram_webhook_path
    await bot.set_webhook(
        url=url,
        secret_token=config.telegram.webhook_secret,
        drop_pending_updates=False,
        allowed_updates=["message", "edited_message", "callback_query"],
    )
    log.info("telegram: webhook at %s", url)
