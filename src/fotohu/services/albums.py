"""Ties :class:`GraphAlbumClient` to persisted settings.

The Graph link for albums is stored encrypted in the same key/value
``settings`` table as everything else (``album_credentials_enc``), the same
way :mod:`fotohu.storage.registry` stores a storage account's tokens — just
without a dedicated table, since there is exactly one of these per install.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.crypto import decrypt_json, encrypt_json
from ..services.settings import SettingsService
from ..storage.graph_albums import Album, GraphAlbumClient

log = logging.getLogger(__name__)


class AlbumService:
    def __init__(self, settings: SettingsService, secret_key: str) -> None:
        self.settings = settings
        self.secret_key = secret_key
        self._client: GraphAlbumClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def linked(self) -> bool:
        return self._client is not None

    async def client(self) -> GraphAlbumClient | None:
        settings = await self.settings.get()
        if not settings.album_credentials_enc:
            return None
        if self._client is None:
            creds = decrypt_json(self.secret_key, settings.album_credentials_enc)
            self._client = GraphAlbumClient(creds)
        return self._client

    async def _persist(self, client: GraphAlbumClient) -> None:
        if not client.credentials_dirty:
            return
        await self.settings.set(
            "album_credentials_enc", encrypt_json(self.secret_key, client.credentials)
        )
        client.credentials_dirty = False

    async def save_link(self, credentials: dict[str, Any]) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = GraphAlbumClient(credentials)
        await self.settings.set(
            "album_credentials_enc", encrypt_json(self.secret_key, credentials)
        )

    async def unlink(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self.settings.update(
            album_credentials_enc=None, album_bundle_id=None, album_name=None,
            album_enabled=False,
        )

    # -------------------------------------------------------------- operations

    async def list_albums(self) -> list[Album]:
        client = await self.client()
        if client is None:
            return []
        albums = await client.list_albums()
        await self._persist(client)
        return albums

    async def create_album(self, name: str) -> Album:
        client = await self.client()
        if client is None:
            raise RuntimeError("no OneDrive album link — connect one first")
        album = await client.create_album(name)
        await self._persist(client)
        return album

    async def choose_album(self, album_id: str, name: str) -> None:
        await self.settings.update(
            album_bundle_id=album_id, album_name=name, album_enabled=True
        )

    async def add_to_default_album(self, drive_item_id: str) -> bool:
        """Best-effort: add one uploaded file to the chosen album.

        Returns whether anything was attempted, so the caller can log
        accordingly; a configuration gap (nothing linked, nothing chosen, or
        the feature switched off) is silently "nothing to do", not an error.
        """
        settings = await self.settings.get()
        if not settings.album_enabled or not settings.album_bundle_id:
            return False
        client = await self.client()
        if client is None:
            return False
        await client.add_item(settings.album_bundle_id, drive_item_id)
        await self._persist(client)
        return True


__all__ = ["AlbumService"]
