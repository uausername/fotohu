"""Google Drive v3.

Two things make Drive different from OneDrive: it addresses folders by id rather
than by path (hence the folder-id cache), and it asks for a resumable session for
anything sizeable. We request the narrow ``drive.file`` scope, so the bot can only
ever see the files it created itself.
"""

from __future__ import annotations

import json
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

AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

SCOPE_APP_ONLY = "https://www.googleapis.com/auth/drive.file"
SCOPE_FULL = "https://www.googleapis.com/auth/drive"

FOLDER_MIME = "application/vnd.google-apps.folder"
SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024
#: Drive requires a multiple of 256 KiB for every chunk but the last.
CHUNK_SIZE = 256 * 1024 * 40  # 10 MiB


class GoogleDriveBackend(OAuthMixin, StorageBackend):
    info = BackendInfo(
        key="gdrive",
        title="Google Drive",
        needs_oauth=True,
        description="Google Drive. По умолчанию запрашивается узкий доступ drive.file — "
                    "бот видит только созданные им самим файлы.",
    )

    token_url = TOKEN
    client_id_env = "GDRIVE_CLIENT_ID"
    client_secret_env = "GDRIVE_CLIENT_SECRET"

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
                f"{cls.client_id_env} is not set — create an OAuth client of type "
                '"Web application" in Google Cloud Console and add its id to .env'
            )
        scope = SCOPE_FULL if (extra or {}).get("full_access") else SCOPE_APP_ONLY
        verifier, challenge = cls._pkce()
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            # Both are required for Google to hand back a refresh token at all.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return AuthStart(url=f"{AUTHORIZE}?{urllib.parse.urlencode(params)}", verifier=verifier)

    @classmethod
    async def finish_auth(
        cls, code: str, verifier: str | None, redirect_uri: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await cls._exchange_code(code=code, verifier=verifier, redirect_uri=redirect_uri)

    # --------------------------------------------------------------------- HTTP

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await self._access_token(self._client)
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        response = await self._client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            self._force_refresh()
            token = await self._access_token(self._client)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._client.request(method, url, headers=headers, **kwargs)
        return response

    @staticmethod
    def _raise_for(response: httpx.Response, context: str) -> None:
        if response.status_code < 400:
            return
        body = response.text[:400]
        if response.status_code in (401, 403):
            if "storageQuotaExceeded" in body:
                raise QuotaExceeded(f"Google Drive is out of space ({context})")
            if "rateLimitExceeded" in body or "userRateLimitExceeded" in body:
                raise RetryableError(f"Drive rate limit ({context})")
            raise StorageAuthError(f"Drive rejected the credentials ({context}): {body}")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(f"Drive {response.status_code} ({context}): {body}")
        raise StorageError(f"Drive {response.status_code} ({context}): {body}")

    @staticmethod
    def _to_remote(data: dict[str, Any], path: str) -> RemoteFile:
        hashes = {}
        if data.get("md5Checksum"):
            hashes["md5"] = data["md5Checksum"]
        if data.get("sha256Checksum"):
            hashes["sha256"] = data["sha256Checksum"]
        size = data.get("size")
        return RemoteFile(
            remote_id=data.get("id", ""),
            path=path,
            size=int(size) if size is not None else None,
            hashes=hashes,
            web_url=data.get("webViewLink"),
        )

    # ------------------------------------------------------------ folder lookup

    @staticmethod
    def _escape(value: str) -> str:
        """Escape a literal for a Drive ``q`` query string."""
        return value.replace("\\", "\\\\").replace("'", "\\'")

    async def _find_child(self, name: str, parent_id: str, folder: bool) -> dict | None:
        clauses = [
            f"name = '{self._escape(name)}'",
            f"'{parent_id}' in parents",
            "trashed = false",
            (f"mimeType = '{FOLDER_MIME}'" if folder else f"mimeType != '{FOLDER_MIME}'"),
        ]
        response = await self._request(
            "GET",
            f"{API}/files",
            params={
                "q": " and ".join(clauses),
                "fields": "files(id,name,size,md5Checksum,sha256Checksum,webViewLink)",
                "pageSize": 1,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        self._raise_for(response, f"lookup {name}")
        files = response.json().get("files") or []
        return files[0] if files else None

    async def _create_folder(self, name: str, parent_id: str) -> str:
        response = await self._request(
            "POST",
            f"{API}/files",
            params={"fields": "id", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        self._raise_for(response, f"mkdir {name}")
        return response.json()["id"]

    async def ensure_folder(self, path: str) -> str:
        cached = await self.cache_get(path)
        if cached:
            return cached

        parent_id = self.extra.get("root_id") or "root"
        current = ""
        for part in [p for p in path.strip("/").split("/") if p]:
            current = f"{current}/{part}" if current else part
            if memo := await self.cache_get(current):
                parent_id = memo
                continue
            found = await self._find_child(part, parent_id, folder=True)
            parent_id = found["id"] if found else await self._create_folder(part, parent_id)
            await self.cache_put(current, parent_id)
        return parent_id

    # --------------------------------------------------------------- operations

    async def exists(self, remote_dir: str, filename: str) -> RemoteFile | None:
        parent_id = await self.ensure_folder(remote_dir)
        found = await self._find_child(filename, parent_id, folder=False)
        if not found:
            return None
        return self._to_remote(found, f"{remote_dir}/{filename}")

    async def upload(self, local: LocalFile, remote_dir: str, filename: str) -> RemoteFile:
        parent_id = await self.ensure_folder(remote_dir)
        path = f"{remote_dir}/{filename}"
        metadata = {"name": filename, "parents": [parent_id]}
        if local.size <= SIMPLE_UPLOAD_LIMIT:
            return await self._upload_multipart(local, metadata, path)
        return await self._upload_resumable(local, metadata, path)

    _FIELDS = "id,name,size,md5Checksum,sha256Checksum,webViewLink"

    async def _upload_multipart(
        self, local: LocalFile, metadata: dict[str, Any], path: str
    ) -> RemoteFile:
        boundary = "fotohu-boundary-7d1a2f"
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                json.dumps(metadata).encode(),
                f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode(),
                local.path.read_bytes(),
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        response = await self._request(
            "POST",
            UPLOAD_API,
            params={"uploadType": "multipart", "fields": self._FIELDS,
                    "supportsAllDrives": "true"},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        self._raise_for(response, f"upload {path}")
        return self._to_remote(response.json(), path)

    async def _upload_resumable(
        self, local: LocalFile, metadata: dict[str, Any], path: str
    ) -> RemoteFile:
        start = await self._request(
            "POST",
            UPLOAD_API,
            params={"uploadType": "resumable", "fields": self._FIELDS,
                    "supportsAllDrives": "true"},
            json=metadata,
            headers={
                "X-Upload-Content-Type": "application/octet-stream",
                "X-Upload-Content-Length": str(local.size),
            },
        )
        self._raise_for(start, f"create session {path}")
        session_url = start.headers.get("Location")
        if not session_url:
            raise StorageError(f"Drive did not return a resumable session URL for {path}")

        total = local.size
        sent = 0
        with local.path.open("rb") as fh:
            while sent < total:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                end = sent + len(chunk) - 1
                # The session URL carries its own auth; no bearer header needed.
                response = await self._client.put(
                    session_url,
                    content=chunk,
                    headers={"Content-Range": f"bytes {sent}-{end}/{total}"},
                )
                if response.status_code in (200, 201):
                    return self._to_remote(response.json(), path)
                if response.status_code == 308:
                    # Drive tells us how much it actually kept; trust it over our
                    # own count so a partially-accepted chunk is re-sent.
                    if rng := response.headers.get("Range"):
                        sent = int(rng.split("-")[-1]) + 1
                        fh.seek(sent)
                    else:
                        sent = end + 1
                    continue
                self._raise_for(response, f"chunk {sent}-{end} of {path}")

        raise StorageError(f"Drive resumable session for {path} ended without a final response")

    async def quota(self) -> Quota | None:
        response = await self._request(
            "GET", f"{API}/about", params={"fields": "storageQuota"}
        )
        if response.status_code >= 400:
            return None
        data = (response.json() or {}).get("storageQuota") or {}
        total = data.get("limit")
        used = data.get("usage")
        return Quota(
            total=int(total) if total is not None else None,
            used=int(used) if used is not None else None,
        )

    async def check(self) -> str:
        response = await self._request(
            "GET", f"{API}/about", params={"fields": "user(displayName,emailAddress)"}
        )
        self._raise_for(response, "check")
        user = (response.json() or {}).get("user") or {}
        await self.ensure_folder(self.root_folder)
        return user.get("emailAddress") or user.get("displayName") or "ok"
