"""Entry point. Runs the bots, the web server and the two workers in one loop."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from .config import Config, load_config
from .context import AppContext, build_context
from .logging import setup_logging

log = logging.getLogger("fotohu")


async def _serve_http(ctx: AppContext, dispatcher=None, bot=None):
    from aiohttp import web

    from .web.app import build_app

    app = build_app(ctx, dispatcher, bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, ctx.config.http_host, ctx.config.http_port)
    await site.start()
    log.info("http listening on %s:%d", ctx.config.http_host, ctx.config.http_port)
    return runner


async def _setup_viber(ctx: AppContext) -> None:
    from .messengers.viber import ViberAdapter, ViberClient

    client = ViberClient(
        token=ctx.config.viber.token or "",
        bot_name=ctx.config.viber.bot_name,
        avatar=ctx.config.viber.avatar,
    )
    ctx.register(ViberAdapter(client))

    if not ctx.config.public_url:
        log.warning(
            "Viber is configured but FOTOHU_PUBLIC_URL is not set — Viber requires a "
            "public HTTPS webhook, so no messages will arrive"
        )
        return

    url = ctx.config.public_url.rstrip("/") + ctx.config.viber_webhook_path
    try:
        await client.set_webhook(url)
        log.info("viber: webhook at %s", url)
    except Exception as exc:  # noqa: BLE001 - a bad Viber setup must not stop Telegram
        log.error("could not register the Viber webhook: %s", exc)


async def run(config: Config) -> None:
    ctx = await build_context(config)

    telegram_bot = telegram_dp = None
    if config.telegram.enabled:
        from .messengers.telegram import bot as tg

        telegram_bot, telegram_dp = await tg.setup(ctx)
    else:
        log.warning("TELEGRAM_BOT_TOKEN is not set — the Telegram side is disabled")

    if config.viber.enabled:
        await _setup_viber(ctx)

    if not ctx.adapters:
        raise SystemExit(
            "No messenger configured. Set TELEGRAM_BOT_TOKEN and/or VIBER_BOT_TOKEN."
        )

    await ctx.start_workers()

    runner = None
    if config.needs_web_server:
        runner = await _serve_http(ctx, telegram_dp, telegram_bot)

    tasks: list[asyncio.Task] = []
    if telegram_bot and telegram_dp:
        from .messengers.telegram import bot as tg

        if config.telegram.use_webhook:
            await tg.install_webhook(telegram_bot, config)
        else:
            tasks.append(
                asyncio.create_task(tg.run_polling(telegram_bot, telegram_dp), name="telegram")
            )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("FotoHu is up: %s", ", ".join(sorted(str(p) for p in ctx.adapters)))
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner:
            await runner.cleanup()
        await ctx.shutdown()


def _use_system_trust_store() -> None:
    """Verify TLS against the OS trust store instead of the bundled CA list.

    Windows machines running an HTTPS-inspecting antivirus (Avast, Kaspersky,
    ESET) or sitting behind a corporate proxy present a re-signed certificate
    whose root lives in the Windows store but not in Python's bundled ``certifi``
    list, so every outbound call — Telegram, Graph, Google — fails verification
    while ``curl`` and ``git`` succeed. ``truststore`` routes verification
    through the OS store, which fixes that without weakening anything: on Linux
    and in the Docker image it simply reads ``/etc/ssl/certs`` instead.
    """
    with contextlib.suppress(ImportError):
        import truststore

        truststore.inject_into_ssl()


def main() -> None:
    _use_system_trust_store()

    parser = argparse.ArgumentParser(prog="fotohu", description="Family photo archiver bot")
    parser.add_argument("--env", default=".env", help="path to the .env file")
    parser.add_argument("--check", action="store_true",
                        help="validate config and database, then exit")
    args = parser.parse_args()

    try:
        config = load_config(args.env)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(config.log_level, config.log_file)

    if args.check:
        asyncio.run(_check(config))
        return

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


async def _check(config: Config) -> None:
    ctx = await build_context(config)
    settings = await ctx.settings.get()
    storage = await ctx.repo.get_default_storage()
    print(f"database:  {config.db_path} ok")
    print(f"people:    {await ctx.repo.count_people()}")
    print(f"telegram:  {'on' if config.telegram.enabled else 'off'}")
    print(f"viber:     {'on' if config.viber.enabled else 'off'}")
    print(f"public url:{config.public_url or ' (not set)'}")
    print(f"storage:   {storage['label'] if storage else '(none configured)'}")
    print(f"layout:    {settings.folder_mode}")
    print(f"purge:     {'on' if settings.purge_enabled else 'off'}, "
          f"{settings.purge_after_hours}h")
    if settings.purge_exceeds_telegram_window:
        print("  warning: purge_after_hours >= 48 — Telegram will refuse those deletions")
    await ctx.shutdown()


if __name__ == "__main__":
    main()
