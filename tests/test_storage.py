"""Cloud backends against mocked HTTP: chunking, verification, token refresh."""

from __future__ import annotations

import hashlib
import time

import httpx
import pytest
import respx

from fotohu.core.errors import QuotaExceeded, RetryableError, StorageAuthError
from fotohu.core.models import LocalFile, RemoteFile
from fotohu.storage.gdrive import GoogleDriveBackend
from fotohu.storage.local import LocalBackend
from fotohu.storage.onedrive import OneDriveBackend


@pytest.fixture
def creds():
    return {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600}


@pytest.fixture
def big_file(tmp_path):
    """Just over OneDrive's 4 MiB simple-upload threshold, to force a session."""
    payload = b"\xff\xd8" + b"P" * (5 * 1024 * 1024)
    path = tmp_path / "big.jpg"
    path.write_bytes(payload)
    return LocalFile(
        path=path,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload).hexdigest(),
        sha1=hashlib.sha1(payload).hexdigest(),
    )


@pytest.fixture
def small_file(tmp_path):
    payload = b"\xff\xd8tiny"
    path = tmp_path / "small.jpg"
    path.write_bytes(payload)
    return LocalFile(
        path=path,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload).hexdigest(),
        sha1=hashlib.sha1(payload).hexdigest(),
    )


class TestVerification:
    """`verify` is what backs the promise that stored bytes match sent bytes."""

    SIZE = len(b"\xff\xd8tiny")

    def make(self, **hashes):
        return RemoteFile(remote_id="1", path="a/b.jpg", size=self.SIZE, hashes=hashes)

    def test_sha256_match(self, small_file):
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, self.make(sha256=small_file.sha256)) is True

    def test_sha256_mismatch_is_detected(self, small_file):
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, self.make(sha256="0" * 64)) is False

    def test_falls_back_to_sha1_for_onedrive_personal(self, small_file):
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, self.make(sha1=small_file.sha1.upper())) is True
        assert backend.verify(small_file, self.make(sha1="0" * 40)) is False

    def test_falls_back_to_md5_for_google_drive(self, small_file):
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, self.make(md5=small_file.md5)) is True

    def test_size_is_the_last_resort(self, small_file):
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, self.make()) is True
        assert backend.verify(small_file, RemoteFile("1", "a", size=999)) is False

    def test_no_signal_at_all_reports_unknown(self, small_file):
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, RemoteFile("1", "a", size=None)) is None

    def test_a_quickxor_only_response_cannot_be_verified(self, small_file):
        # OneDrive for Business reports only quickXorHash, which we do not compute;
        # falling through to size is honest, and the UI says "unverified".
        backend = LocalBackend(1, "root", {})
        assert backend.verify(small_file, self.make(quickxor="abc")) is True


class TestOneDrive:
    GRAPH = "https://graph.microsoft.com/v1.0"

    @respx.mock
    async def test_a_small_file_goes_up_in_one_put(self, creds, small_file):
        backend = OneDriveBackend(1, "FotoHu", creds)
        respx.get(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(200, json={"id": "folder-1"})
        )
        route = respx.put(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "file-1", "size": small_file.size, "webUrl": "https://od/x",
                    "file": {"hashes": {"sha256Hash": small_file.sha256.upper()}},
                },
            )
        )

        remote = await backend.upload(small_file, "FotoHu", "a.jpg")
        assert route.called
        assert remote.remote_id == "file-1"
        assert backend.verify(small_file, remote) is True
        await backend.close()

    @respx.mock
    async def test_a_large_file_uses_a_resumable_session(self, creds, big_file, monkeypatch):
        # Shrink the chunk to the Graph minimum so a 5 MiB file spans many chunks.
        from fotohu.storage import onedrive

        monkeypatch.setattr(onedrive, "CHUNK_SIZE", 320 * 1024)
        backend = OneDriveBackend(1, "FotoHu", creds)
        respx.get(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(200, json={"id": "folder-1"})
        )
        respx.post(url__regex=r".*createUploadSession$").mock(
            return_value=httpx.Response(200, json={"uploadUrl": "https://upload.test/session"})
        )
        chunks: list[str] = []

        bodies: list[int] = []

        def on_chunk(request: httpx.Request) -> httpx.Response:
            chunks.append(request.headers["Content-Range"])
            bodies.append(len(request.content))
            sent = sum(bodies)
            if sent < big_file.size:
                return httpx.Response(202, json={})
            return httpx.Response(
                201,
                json={
                    "id": "file-2", "size": big_file.size,
                    "file": {"hashes": {"sha1Hash": big_file.sha1}},
                },
            )

        respx.put("https://upload.test/session").mock(side_effect=on_chunk)

        remote = await backend.upload(big_file, "FotoHu", "big.jpg")

        assert len(chunks) > 1, "a 5 MiB file should not fit in one 320 KiB chunk"
        assert chunks[0].startswith("bytes 0-")
        # Graph rejects any chunk but the last that is not a multiple of 320 KiB.
        assert all(size % (320 * 1024) == 0 for size in bodies[:-1])
        # The ranges must tile the file exactly, with no gap and no overlap.
        assert sum(bodies) == big_file.size
        assert chunks[-1].endswith(f"/{big_file.size}")
        assert remote.remote_id == "file-2"
        await backend.close()

    @respx.mock
    async def test_a_401_triggers_exactly_one_refresh_and_retry(self, small_file):
        stale = {"access_token": "old", "refresh_token": "rt", "expires_at": time.time() + 3600}
        backend = OneDriveBackend(1, "FotoHu", stale)

        import os

        os.environ["ONEDRIVE_CLIENT_ID"] = "test-client"
        refresh = respx.post("https://login.microsoftonline.com/common/oauth2/v2.0/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "fresh", "refresh_token": "rt2", "expires_in": 3600},
            )
        )
        responses = [httpx.Response(401, json={}), httpx.Response(200, json={"id": "f"})]
        respx.get(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            side_effect=lambda r: responses.pop(0)
        )

        await backend.ensure_folder("FotoHu")
        assert refresh.called
        assert backend.credentials["access_token"] == "fresh"
        # The rotated refresh token must be persisted, or the link dies later.
        assert backend.credentials["refresh_token"] == "rt2"
        assert backend.credentials_dirty is True
        await backend.close()

    @respx.mock
    async def test_a_full_drive_raises_quota_exceeded(self, creds, small_file):
        backend = OneDriveBackend(1, "FotoHu", creds)
        respx.get(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(200, json={"id": "folder-1"})
        )
        respx.put(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(507, text="quotaLimitReached")
        )
        with pytest.raises(QuotaExceeded):
            await backend.upload(small_file, "FotoHu", "a.jpg")
        await backend.close()

    @respx.mock
    async def test_a_server_error_is_marked_retryable(self, creds, small_file):
        backend = OneDriveBackend(1, "FotoHu", creds)
        respx.get(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(200, json={"id": "folder-1"})
        )
        respx.put(url__startswith=f"{self.GRAPH}/me/drive/root:/FotoHu").mock(
            return_value=httpx.Response(503, text="try later")
        )
        with pytest.raises(RetryableError):
            await backend.upload(small_file, "FotoHu", "a.jpg")
        await backend.close()


class TestGoogleDrive:
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

    @respx.mock
    async def test_folders_are_created_once_and_then_cached(self, creds, ctx):
        account_id = await ctx.repo.create_storage_account("gdrive", "GDrive")
        backend = await ctx.storage.build(
            {**(await ctx.repo.get_storage_account(account_id)), "credentials_enc": None}
        )
        backend.credentials = creds

        lookups = respx.get(f"{self.API}/files").mock(
            return_value=httpx.Response(200, json={"files": []})
        )
        creates = respx.post(f"{self.API}/files").mock(
            side_effect=lambda r: httpx.Response(200, json={"id": "folder-x"})
        )

        assert await backend.ensure_folder("FotoHu/dad/2026") == "folder-x"
        assert creates.call_count == 3  # FotoHu, dad, 2026

        # Second call must be served entirely from the persisted cache.
        lookups.reset()
        creates.reset()
        assert await backend.ensure_folder("FotoHu/dad/2026") == "folder-x"
        assert lookups.call_count == 0 and creates.call_count == 0
        await backend.close()

    @respx.mock
    async def test_resumable_upload_resumes_from_the_offset_drive_reports(
        self, creds, big_file
    ):
        backend = GoogleDriveBackend(1, "FotoHu", creds)
        respx.get(f"{self.API}/files").mock(
            return_value=httpx.Response(200, json={"files": [{"id": "folder-1"}]})
        )
        respx.post(self.UPLOAD).mock(
            return_value=httpx.Response(200, headers={"Location": "https://upload.test/s"})
        )

        ranges: list[str] = []
        # Accept only the first 1 MiB, then demand the rest — the classic partial write.
        state = {"first": True}

        def on_chunk(request: httpx.Request) -> httpx.Response:
            ranges.append(request.headers["Content-Range"])
            if state["first"]:
                state["first"] = False
                return httpx.Response(308, headers={"Range": "bytes=0-1048575"})
            return httpx.Response(
                200,
                json={"id": "g-1", "size": str(big_file.size),
                      "md5Checksum": big_file.md5},
            )

        respx.put("https://upload.test/s").mock(side_effect=on_chunk)

        remote = await backend.upload(big_file, "FotoHu", "big.jpg")
        assert len(ranges) == 2
        # The second chunk must restart at exactly what Drive kept, not at our count.
        assert ranges[1].startswith("bytes 1048576-")
        assert backend.verify(big_file, remote) is True
        await backend.close()

    @respx.mock
    async def test_a_small_file_goes_up_as_multipart(self, creds, small_file):
        backend = GoogleDriveBackend(1, "FotoHu", creds)
        respx.get(f"{self.API}/files").mock(
            return_value=httpx.Response(200, json={"files": [{"id": "folder-1"}]})
        )
        route = respx.post(self.UPLOAD).mock(
            return_value=httpx.Response(
                200, json={"id": "g-2", "size": str(small_file.size),
                           "md5Checksum": small_file.md5}
            )
        )
        remote = await backend.upload(small_file, "FotoHu", "a.jpg")
        assert "multipart" in str(route.calls[0].request.url)
        assert backend.verify(small_file, remote) is True
        await backend.close()

    @respx.mock
    async def test_revoked_credentials_raise_storage_auth_error(self, creds):
        backend = GoogleDriveBackend(1, "FotoHu", creds)
        respx.get(f"{self.API}/files").mock(
            return_value=httpx.Response(403, text='{"error":{"message":"forbidden"}}')
        )
        with pytest.raises(StorageAuthError):
            await backend.ensure_folder("FotoHu")
        await backend.close()

    @respx.mock
    async def test_a_full_drive_raises_quota_exceeded(self, creds):
        backend = GoogleDriveBackend(1, "FotoHu", creds)
        respx.get(f"{self.API}/files").mock(
            return_value=httpx.Response(403, text='{"error":"storageQuotaExceeded"}')
        )
        with pytest.raises(QuotaExceeded):
            await backend.ensure_folder("FotoHu")
        await backend.close()

    def test_query_literals_are_escaped(self):
        # A folder named  O'Brien  must not break out of the q= expression.
        assert GoogleDriveBackend._escape("O'Brien") == "O\\'Brien"


class TestAuthUrls:
    def test_onedrive_asks_for_offline_access(self, monkeypatch):
        monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
        start = OneDriveBackend.begin_auth("https://x.test/oauth/callback")
        assert "offline_access" in start.url and "Files.ReadWrite" in start.url
        assert "code_challenge_method=S256" in start.url
        assert start.verifier

    def test_google_asks_for_offline_access_and_the_narrow_scope(self, monkeypatch):
        monkeypatch.setenv("GDRIVE_CLIENT_ID", "cid")
        start = GoogleDriveBackend.begin_auth("https://x.test/oauth/callback")
        assert "access_type=offline" in start.url
        assert "prompt=consent" in start.url  # or Google withholds the refresh token
        assert "drive.file" in start.url

    def test_google_full_access_is_opt_in(self, monkeypatch):
        monkeypatch.setenv("GDRIVE_CLIENT_ID", "cid")
        start = GoogleDriveBackend.begin_auth(
            "https://x.test/oauth/callback", {"full_access": True}
        )
        assert "drive.file" not in start.url
