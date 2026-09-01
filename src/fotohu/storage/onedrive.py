"""Microsoft OneDrive via the Graph API.

Files under 4 MiB go up in a single PUT; anything larger uses an upload session
with 10 MiB chunks (Graph requires a multiple of 320 KiB) so a dropped
connection costs one chunk rather than the whole photo.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any

import httpx

from ..core.errors import QuotaExceeded, RetryableError, StorageAuthError, StorageError
from ..core.models import LocalFile, Quota, RemoteFile
from .base import AuthStart, BackendInfo, StorageBackend
from .oauth import OAuthMixin

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORIZE = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPES = "offline_access Files.ReadWrite User.Read"

SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
#: Graph mandates a multiple of 320 KiB for every chunk but the last.
CHUNK_SIZE = 320 * 1024 * 32  # 10 MiB


class OneDriveBackend(OAuthMixin, StorageBackend):
    info = BackendInfo(
        key="onedrive",
        title="Microsoft OneDrive",
        needs_oauth=True,
        description="Личный OneDrive или OneDrive for Business через Microsoft Graph.",
    )

    token_url = TOKEN
    client_id_env = "ONEDRIVE_CLIENT_ID"
    client_secret_env = "ONEDRIVE_CLIENT_SECRET"

    def __init__(self, account_id: int, root_folder: str, credentials: dict[str, Any],
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(account_id, root_folder, credentials, extra)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0))

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ linking

    @classmethod
    def begin_auth(cls, redirect_uri: str, extra: dict[str, Any] | None = None) -> AuthStart:
        client_id = os.getenv(cls.client_id_env, "").strip()
        if not client_id:
            raise StorageError(
                f"{cls.client_id_env} is not set — register an app at "
                "https://entra.microsoft.com and put its Application (client) ID in .env"
            )
        verifier, challenge = cls._pkce()
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return AuthStart(url=f"{AUTHORIZE}?{urllib.parse.urlencode(params)}", verifier=verifier)

    @classmethod
    async def finish_auth(
        cls, code: str, verifier: str | None, redirect_uri: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await cls._exchange_code(
            code=code, verifier=verifier, redirect_uri=redirect_uri, scope=SCOPES
        )

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
            raise StorageAuthError(f"OneDrive rejected the credentials ({context}): {body}")
        if response.status_code == 507 or "quotaLimitReached" in body:
            raise QuotaExceeded(f"OneDrive is out of space ({context})")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(f"OneDrive {response.status_code} ({context}): {body}")
        raise StorageError(f"OneDrive {response.status_code} ({context}): {body}")

    @staticmethod
    def _item_url(path: str) -> str:
        """Graph addresses items by path with a ``root:/a/b:`` colon syntax."""
        clean = "/".join(urllib.parse.quote(p, safe="") for p in path.strip("/").split("/") if p)
        return f"{GRAPH}/me/drive/root:/{clean}" if clean else f"{GRAPH}/me/drive/root"

    @staticmethod
    def _to_remote(data: dict[str, Any], path: str) -> RemoteFile:
        raw = (data.get("file") or {}).get("hashes") or {}
        hashes = {}
        if raw.get("sha256Hash"):
            hashes["sha256"] = raw["sha256Hash"]
        if raw.get("sha1Hash"):
            hashes["sha1"] = raw["sha1Hash"]
        if raw.get("quickXorHash"):
            hashes["quickxor"] = raw["quickXorHash"]
        return RemoteFile(
            remote_id=data.get("id", ""),
            path=path,
            size=data.get("size"),
            hashes=hashes,
            web_url=data.get("webUrl"),
        )

    # -------------------------------------------------------------- operations

    async def ensure_folder(self, path: str) -> str:
        """Walk the path creating each missing level. OneDrive is path-addressed,
        so this is only needed to make the parents exist before an upload."""
        parts = [p for p in path.strip("/").split("/") if p]
        current = ""
        remote_id = ""
        for part in parts:
            parent = current
            current = f"{current}/{part}" if current else part
            response = await self._request("GET", f"{self._item_url(current)}?$select=id")
            if response.status_code == 200:
                remote_id = response.json().get("id", "")
                continue
            if response.status_code != 404:
                self._raise_for(response, f"stat {current}")

            create_url = (
                f"{self._item_url(parent)}:/children"
                if parent
                else f"{GRAPH}/me/drive/root/children"
            )
            created = await self._request(
                "POST",
                create_url,
                json={
                    "name": part,
                    "folder": {},
                    # Another worker may have created it a millisecond ago.
                    "@microsoft.graph.conflictBehavior": "replace",
                },
            )
            if created.status_code == 409:
                continue
            self._raise_for(created, f"mkdir {current}")
            remote_id = created.json().get("id", "")
        return remote_id

    async def exists(self, remote_dir: str, filename: str) -> RemoteFile | None:
        path = f"{remote_dir}/{filename}"
        response = await self._request(
            "GET", f"{self._item_url(path)}?$select=id,name,size,file,webUrl"
        )
        if response.status_code == 404:
            return None
        self._raise_for(response, f"stat {path}")
        return self._to_remote(response.json(), path)

    async def upload(self, local: LocalFile, remote_dir: str, filename: str) -> RemoteFile:
        await self.ensure_folder(remote_dir)
        path = f"{remote_dir}/{filename}"
        if local.size <= SIMPLE_UPLOAD_LIMIT:
            return await self._upload_simple(local, path)
        return await self._upload_session(local, path)

    async def _upload_simple(self, local: LocalFile, path: str) -> RemoteFile:
        with local.path.open("rb") as fh:
            response = await self._request(
                "PUT",
                f"{self._item_url(path)}:/content?@microsoft.graph.conflictBehavior=fail",
                content=fh.read(),
                headers={"Content-Type": "application/octet-stream"},
            )
        self._raise_for(response, f"upload {path}")
        return self._to_remote(response.json(), path)

    async def _upload_session(self, local: LocalFile, path: str) -> RemoteFile:
        session = await self._request(
            "POST",
            f"{self._item_url(path)}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "fail"}},
        )
        self._raise_for(session, f"create session {path}")
        upload_url = session.json()["uploadUrl"]

        total = local.size
        sent = 0
        with local.path.open("rb") as fh:
            while sent < total:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                end = sent + len(chunk) - 1
                # The session URL is pre-authorised — no bearer token on chunks.
                response = await self._client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{total}",
                    },
                )
                if response.status_code in (200, 201):
                    return self._to_remote(response.json(), path)
                if response.status_code != 202:
                    await self._client.delete(upload_url)
                    self._raise_for(response, f"chunk {sent}-{end} of {path}")
                sent = end + 1

        raise StorageError(f"OneDrive upload session for {path} ended without a final response")

    async def quota(self) -> Quota | None:
        response = await self._request("GET", f"{GRAPH}/me/drive?$select=quota")
        if response.status_code >= 400:
            return None
        data = (response.json() or {}).get("quota") or {}
        return Quota(total=data.get("total"), used=data.get("used"))

    async def check(self) -> str:
        response = await self._request("GET", f"{GRAPH}/me/drive?$select=id,driveType,owner")
        self._raise_for(response, "check")
        data = response.json()
        await self.ensure_folder(self.root_folder)
        owner = ((data.get("owner") or {}).get("user") or {}).get("displayName", "?")
        return f"{data.get('driveType', 'drive')} / {owner}"
