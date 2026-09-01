"""Minimal Viber REST client.

The whole public bot API is six endpoints, so a thin httpx wrapper beats pulling
in the ageing synchronous SDK. Note what is *not* here: Viber offers no way for a
bot to delete a message, which is why chat cleanup is Telegram-only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from pathlib import Path
from typing import Any

import httpx

from ...core.download import stream_to_file
from ...core.errors import DownloadError, RetryableError
from ...core.models import LocalFile, Platform
from ..base import MessengerAdapter

log = logging.getLogger(__name__)

API = "https://chatapi.viber.com/pa"

#: Viber accepts files up to 200 MB from users.
MAX_FILE_BYTES = 200 * 1024 * 1024


def verify_signature(token: str, body: bytes, signature: str | None) -> bool:
    """Check ``X-Viber-Content-Signature``: HMAC-SHA256 of the raw body.

    Without this anyone who learns the webhook URL could post events as Viber.
    """
    if not signature:
        return False
    expected = hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class ViberClient:
    def __init__(self, token: str, bot_name: str = "FotoHu", avatar: str | None = None) -> None:
        self.token = token
        self.bot_name = bot_name
        self.avatar = avatar
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=300.0),
            headers={"X-Viber-Auth-Token": token},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"{API}/{endpoint}", json=payload)
        if response.status_code >= 500 or response.status_code == 429:
            raise RetryableError(f"viber {endpoint}: HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        # Viber signals errors in the body with status != 0, not via HTTP codes.
        if data.get("status") != 0:
            raise RuntimeError(
                f"viber {endpoint} failed: {data.get('status')} {data.get('status_message')}"
            )
        return data

    async def set_webhook(self, url: str, events: list[str] | None = None) -> dict[str, Any]:
        return await self._post(
            "set_webhook",
            {
                "url": url,
                "event_types": events
                or ["delivered", "seen", "failed", "subscribed", "unsubscribed",
                    "conversation_started"],
                "send_name": True,
                "send_photo": True,
            },
        )

    async def remove_webhook(self) -> dict[str, Any]:
        return await self._post("set_webhook", {"url": ""})

    async def account_info(self) -> dict[str, Any]:
        return await self._post("get_account_info", {})

    async def send_text(self, receiver: str, text: str) -> str | None:
        data = await self._post(
            "send_message",
            {
                "receiver": receiver,
                "type": "text",
                "text": text,
                "sender": {
                    "name": self.bot_name,
                    **({"avatar": self.avatar} if self.avatar else {}),
                },
            },
        )
        token = data.get("message_token")
        return str(token) if token else None

    async def download(self, url: str, dest: Path, size_limit: int | None = None) -> LocalFile:
        async def chunks():
            async with self._client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise DownloadError(
                        f"viber media {response.status_code} — the link may have expired"
                    )
                async for chunk in response.aiter_bytes(256 * 1024):
                    yield chunk

        return await stream_to_file(chunks(), dest, size_limit=size_limit)


class ViberAdapter(MessengerAdapter):
    platform = Platform.VIBER
    #: Viber's REST bot API has no delete method at all — see docs/limitations.md.
    supports_deletion = False
    delete_window_hours = None
    download_limit = MAX_FILE_BYTES

    def __init__(self, client: ViberClient) -> None:
        self.client = client

    async def download(
        self, file_ref: str, dest: Path, size_limit: int | None = None
    ) -> LocalFile:
        return await self.client.download(file_ref, dest, size_limit)

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str | None:
        # Viber has no reply threading for bots; reply_to is accepted and ignored.
        return await self.client.send_text(chat_id, _strip_html(text))

    async def close(self) -> None:
        await self.client.close()


def _strip_html(text: str) -> str:
    """Our texts are written for Telegram's HTML; Viber renders plain text only."""
    import re

    text = re.sub(r"<br\s*/?>", "\n", text)
    return re.sub(r"</?[a-zA-Z][^>]*>", "", text)
