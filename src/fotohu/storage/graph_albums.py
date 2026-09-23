"""OneDrive albums via Microsoft Graph — separate from the file backend.

An "album" in the OneDrive/Photos sense is a Graph ``bundle`` resource
(``/me/drive/bundles`` with an ``album`` facet), not a folder. It exists only
for personal Microsoft accounts, and neither rclone's onedrive backend nor
:class:`fotohu.storage.onedrive.OneDriveBackend` touches it — the file
backends only ever see the drive's file hierarchy. When uploads go up through
rclone (the common case here — see ``docs/setup-onedrive.md``), the bot also
has no Graph token of its own to call this with, since rclone keeps its OAuth
token to itself.

So this is a second, minimal OAuth link, acquired once through the
device-code flow — no redirect URI, no public domain needed, just a code
typed in at https://microsoft.com/devicelogin — kept only for two calls:
listing/creating albums, and adding an already-uploaded file's ``driveItem``
to one by id.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..core.errors import StorageAuthError, StorageError
from .oauth import OAuthMixin

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
DEVICE_CODE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
#: Same app registration as the file backend (docs/setup-onedrive.md), just a
#: second, independent grant — Files.ReadWrite is also what bundle writes need.
SCOPES = "offline_access Files.ReadWrite"


@dataclass(slots=True)
class DeviceAuth:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    started_at: float


@dataclass(slots=True)
class Album:
    id: str
    name: str


class GraphAlbumClient(OAuthMixin):
    """Talks to ``/me/drive/bundles``. Personal OneDrive accounts only."""

    token_url = TOKEN_URL
    client_id_env = "ONEDRIVE_CLIENT_ID"
    client_secret_env = "ONEDRIVE_CLIENT_SECRET"

    def __init__(self, credentials: dict[str, Any]) -> None:
        self.credentials = dict(credentials)
        self.credentials_dirty = False
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ device flow

    @classmethod
    async def begin_device_auth(cls) -> DeviceAuth:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                DEVICE_CODE_URL, data={"client_id": cls._client_id(), "scope": SCOPES}
            )
        if response.status_code >= 400:
            raise StorageAuthError(f"could not start device sign-in: {response.text[:400]}")
        data = response.json()
        return DeviceAuth(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data.get("verification_uri", "https://microsoft.com/devicelogin"),
            interval=int(data.get("interval", 5)),
            expires_in=int(data.get("expires_in", 900)),
            started_at=time.monotonic(),
        )

    @classmethod
    async def poll_device_auth(cls, auth: DeviceAuth) -> dict[str, Any]:
        """Poll until the admin finishes signing in, or the code expires.

        Raises :class:`StorageAuthError` on expiry or denial; otherwise returns
        the credentials dict to persist.
        """
        payload: dict[str, Any] = {
            "client_id": cls._client_id(),
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": auth.device_code,
        }
        if secret := cls._client_secret():
            payload["client_secret"] = secret

        deadline = auth.started_at + auth.expires_in
        interval = max(auth.interval, 1)
        async with httpx.AsyncClient(timeout=30.0) as client:
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                response = await client.post(TOKEN_URL, data=payload)
                body = response.json()
                if response.status_code < 400:
                    if not body.get("refresh_token"):
                        raise StorageAuthError(
                            "Microsoft returned no refresh token for the device sign-in"
                        )
                    return cls._store_tokens(body)
                error = body.get("error")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                raise StorageAuthError(
                    f"device sign-in failed: {body.get('error_description', error)}"
                )
        raise StorageAuthError("the device sign-in code expired before it was confirmed")

    # -------------------------------------------------------------------- HTTP

    async def _headers(self) -> dict[str, str]:
        token = await self._access_token(self._client)
        return {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**(await self._headers()), **kwargs.pop("headers", {})}
        response = await self._client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            # The cached token expired mid-flight; force one refresh and retry.
            self._force_refresh()
            headers = {**(await self._headers()), **kwargs.pop("headers", {})}
            response = await self._client.request(method, url, headers=headers, **kwargs)
        return response

    @staticmethod
    def _raise_for(response: httpx.Response, context: str) -> None:
        if response.status_code < 400:
            return
        body = response.text[:400]
        if response.status_code in (401, 403):
            raise StorageAuthError(f"OneDrive rejected the album request ({context}): {body}")
        raise StorageError(f"OneDrive album request failed ({context}): {body}")

    # -------------------------------------------------------------- operations

    async def list_albums(self) -> list[Album]:
        response = await self._request("GET", f"{GRAPH}/me/drive/bundles?$select=id,name,bundle")
        self._raise_for(response, "list albums")
        raw = response.json().get("value") or []
        # TEMPORARY: the /bundles contract is thin on official docs — log the
        # raw shape once so a mismatch (deprecated facet, wrong endpoint, ...)
        # is visible in the deploy logs instead of just "no albums found".
        log.warning("graph /me/drive/bundles raw response (%d item(s)): %r", len(raw), raw)
        albums = []
        for item in raw:
            if (item.get("bundle") or {}).get("album") is not None:
                albums.append(Album(id=item["id"], name=item.get("name", "?")))
        return albums

    async def create_album(self, name: str) -> Album:
        response = await self._request(
            "POST", f"{GRAPH}/me/drive/bundles", json={"name": name, "bundle": {"album": {}}}
        )
        self._raise_for(response, f"create album {name!r}")
        data = response.json()
        return Album(id=data["id"], name=data.get("name", name))

    async def add_item(self, album_id: str, drive_item_id: str) -> None:
        response = await self._request(
            "PATCH",
            f"{GRAPH}/me/drive/items/{album_id}",
            json={"children@odata.bind": [f"{GRAPH}/me/drive/items/{drive_item_id}"]},
        )
        self._raise_for(response, f"add {drive_item_id} to album {album_id}")


__all__ = ["GraphAlbumClient", "DeviceAuth", "Album"]
