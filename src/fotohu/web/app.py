"""The HTTP surface: Viber's webhook, Telegram's optional webhook, OAuth callback."""

from __future__ import annotations

import logging

from aiohttp import web

from ..context import AppContext
from ..messengers.viber import handle_event, verify_signature
from .oauth import oauth_callback

log = logging.getLogger(__name__)


async def health(request: web.Request) -> web.Response:
    ctx: AppContext = request.app["ctx"]
    stats = await ctx.repo.stats()
    queued = sum(
        stats["by_state"].get(state, {}).get("count", 0) for state in ("pending", "uploading")
    )
    return web.json_response({"status": "ok", "queued": queued})


async def viber_webhook(request: web.Request) -> web.Response:
    ctx: AppContext = request.app["ctx"]
    body = await request.read()

    # Viber signs every delivery; an unsigned request is not from Viber.
    if not verify_signature(
        ctx.config.viber.token or "", body, request.headers.get("X-Viber-Content-Signature")
    ):
        log.warning("rejected a Viber callback with a bad signature")
        return web.Response(status=403, text="bad signature")

    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad json")

    # Viber's webhook registration probe expects a 200 before anything else.
    if event.get("event") == "webhook":
        return web.json_response({"status": 0})

    try:
        await handle_event(ctx, event)
    except Exception:  # noqa: BLE001 - never let Viber retry-storm us over a bug
        log.exception("viber event handling failed: %s", event.get("event"))
    return web.json_response({"status": 0})


def telegram_webhook_handler(dispatcher, bot):
    """Feed Telegram updates into the same aiogram dispatcher polling would use."""
    from aiogram.types import Update

    async def handler(request: web.Request) -> web.Response:
        ctx: AppContext = request.app["ctx"]
        secret = ctx.config.telegram.webhook_secret
        if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return web.Response(status=403, text="bad secret")
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="bad json")
        await dispatcher.feed_update(bot, Update.model_validate(data, context={"bot": bot}))
        return web.Response(text="ok")

    return handler


def build_app(ctx: AppContext, dispatcher=None, bot=None) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["ctx"] = ctx

    app.router.add_get("/health", health)
    app.router.add_get(ctx.config.oauth_redirect_path, oauth_callback)

    if ctx.config.viber.enabled:
        app.router.add_post(ctx.config.viber_webhook_path, viber_webhook)

    if dispatcher is not None and bot is not None and ctx.config.telegram.use_webhook:
        app.router.add_post(
            ctx.config.telegram_webhook_path, telegram_webhook_handler(dispatcher, bot)
        )
    return app
