"""OAuth redirect endpoint.

The admin taps a link in the chat, consents in their browser, and the provider
sends them back here. The ``state`` is single-use and short-lived, so a leaked
link cannot be replayed.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..context import AppContext

log = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FotoHu</title>
<style>
  body{{font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       display:grid;place-items:center;min-height:100vh;margin:0;
       background:#f6f7f9;color:#1a1d21}}
  .card{{background:#fff;padding:2rem 2.5rem;border-radius:14px;max-width:32rem;
        box-shadow:0 1px 3px rgba(0,0,0,.08),0 8px 24px rgba(0,0,0,.06);text-align:center}}
  .icon{{font-size:2.5rem;line-height:1}}
  h1{{font-size:1.25rem;margin:.75rem 0 .5rem}}
  p{{margin:.25rem 0;color:#5b626b}}
  code{{background:#f0f1f3;padding:.15em .4em;border-radius:4px;font-size:.9em}}
</style></head>
<body><div class="card">
  <div class="icon">{icon}</div><h1>{title}</h1><p>{body}</p>
</div></body></html>"""


def _page(icon: str, title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=PAGE.format(icon=icon, title=title, body=body),
        content_type="text/html",
        status=status,
    )


async def oauth_callback(request: web.Request) -> web.Response:
    ctx: AppContext = request.app["ctx"]

    if error := request.query.get("error"):
        description = request.query.get("error_description", "")
        return _page("⚠️", "Доступ не выдан", f"{error}. {description}", status=400)

    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return _page("⚠️", "Неполный ответ", "В ссылке нет code или state.", status=400)

    record = await ctx.repo.consume_oauth_state(state)
    if record is None:
        return _page(
            "⏳", "Ссылка недействительна",
            "Она одноразовая и живёт 10 минут. Откройте «Подключить аккаунт» "
            "в боте ещё раз.",
            status=400,
        )

    redirect_uri = ctx.config.oauth_redirect_uri
    account_id = (record.get("payload") or {}).get("account_id")
    if not redirect_uri or not account_id:
        return _page("⚠️", "Ошибка конфигурации", "Не задан FOTOHU_PUBLIC_URL.", status=500)

    try:
        credentials = await ctx.storage.finish_auth(
            record["backend"], code, record.get("verifier"), redirect_uri
        )
        await ctx.storage.save_credentials(account_id, credentials)
    except Exception as exc:  # noqa: BLE001
        log.exception("oauth exchange failed for %s", record["backend"])
        return _page("❌", "Не удалось получить токен", str(exc)[:300], status=502)

    # A freshly linked account is almost certainly the one they want to use.
    if not await ctx.repo.get_default_storage():
        await ctx.repo.set_default_storage(account_id)
    if ctx.uploader:
        ctx.uploader.notify()

    account = await ctx.repo.get_storage_account(account_id)
    label = (account or {}).get("label", record["backend"])
    log.info("linked storage account %s (%s)", account_id, record["backend"])
    return _page(
        "✅",
        "Готово",
        f"Аккаунт <b>{label}</b> подключён. Можно вернуться в бот и присылать фотографии.",
    )
