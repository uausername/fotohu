"""OneDrive albums via Graph: device sign-in, listing, creating, adding items."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from fotohu.core.crypto import decrypt_json
from fotohu.core.errors import StorageAuthError, StorageError
from fotohu.storage.graph_albums import DeviceAuth, GraphAlbumClient

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
DEVICE_CODE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"


@pytest.fixture(autouse=True)
def client_id(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "test-client")


@pytest.fixture
def creds():
    return {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600}


class TestDeviceFlow:
    @respx.mock
    async def test_begin_device_auth_returns_the_code_to_show(self):
        respx.post(DEVICE_CODE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "dc-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 1,
                    "expires_in": 900,
                },
            )
        )
        auth = await GraphAlbumClient.begin_device_auth()
        assert auth.user_code == "ABCD-EFGH"
        assert auth.interval == 1

    @respx.mock
    async def test_poll_device_auth_waits_out_pending_then_returns_tokens(self):
        auth = DeviceAuth(
            device_code="dc-1", user_code="X", verification_uri="https://x",
            interval=0, expires_in=5, started_at=time.monotonic(),
        )
        responses = [
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            ),
        ]
        respx.post(TOKEN_URL).mock(side_effect=lambda r: responses.pop(0))
        credentials = await GraphAlbumClient.poll_device_auth(auth)
        assert credentials["refresh_token"] == "rt"

    @respx.mock
    async def test_poll_device_auth_raises_on_denial(self):
        auth = DeviceAuth(
            device_code="dc-1", user_code="X", verification_uri="https://x",
            interval=0, expires_in=5, started_at=time.monotonic(),
        )
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "authorization_declined"})
        )
        with pytest.raises(StorageAuthError):
            await GraphAlbumClient.poll_device_auth(auth)

    async def test_poll_device_auth_raises_once_the_code_is_already_expired(self):
        auth = DeviceAuth(
            device_code="dc-1", user_code="X", verification_uri="https://x",
            interval=1, expires_in=0, started_at=time.monotonic() - 1,
        )
        with pytest.raises(StorageAuthError):
            await GraphAlbumClient.poll_device_auth(auth)


class TestAlbumOperations:
    @respx.mock
    async def test_list_albums_keeps_only_bundles_with_an_album_facet(self, creds):
        client = GraphAlbumClient(creds)
        respx.get(f"{GRAPH}/me/drive/bundles").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "1", "name": "Family", "bundle": {"album": {}}},
                        {"id": "2", "name": "Not an album", "bundle": {}},
                    ]
                },
            )
        )
        albums = await client.list_albums()
        assert [a.id for a in albums] == ["1"]
        assert albums[0].name == "Family"
        await client.close()

    @respx.mock
    async def test_create_album_posts_the_album_facet(self, creds):
        client = GraphAlbumClient(creds)
        route = respx.post(f"{GRAPH}/me/drive/bundles").mock(
            return_value=httpx.Response(200, json={"id": "9", "name": "Trip"})
        )
        album = await client.create_album("Trip")
        assert album.id == "9"
        assert route.calls[0].request.content
        await client.close()

    @respx.mock
    async def test_add_item_binds_the_existing_driveitem_to_the_bundle(self, creds):
        client = GraphAlbumClient(creds)
        route = respx.patch(f"{GRAPH}/me/drive/items/album-1").mock(
            return_value=httpx.Response(200, json={})
        )
        await client.add_item("album-1", "photo-1")
        import json as _json

        body = _json.loads(route.calls[0].request.content)
        assert body["children@odata.bind"] == [f"{GRAPH}/me/drive/items/photo-1"]
        await client.close()

    @respx.mock
    async def test_a_rejected_add_raises_instead_of_failing_silently(self, creds):
        client = GraphAlbumClient(creds)
        respx.patch(f"{GRAPH}/me/drive/items/album-1").mock(
            return_value=httpx.Response(404, text="not found")
        )
        with pytest.raises(StorageError):
            await client.add_item("album-1", "photo-1")
        await client.close()


class TestAlbumService:
    async def test_add_to_default_album_is_a_noop_until_configured(self, ctx):
        added = await ctx.albums.add_to_default_album("photo-1")
        assert added is False

    @respx.mock
    async def test_add_to_default_album_goes_through_once_linked_and_chosen(self, ctx, creds):
        await ctx.albums.save_link(creds)
        await ctx.albums.choose_album("album-1", "Family")
        route = respx.patch(f"{GRAPH}/me/drive/items/album-1").mock(
            return_value=httpx.Response(200, json={})
        )
        added = await ctx.albums.add_to_default_album("photo-1")
        assert added is True
        assert route.called

    async def test_save_link_persists_the_credentials_encrypted(self, ctx, creds):
        await ctx.albums.save_link(creds)
        settings = await ctx.settings.get()
        assert settings.album_credentials_enc
        stored = decrypt_json(ctx.config.secret_key, settings.album_credentials_enc)
        assert stored["refresh_token"] == creds["refresh_token"]

    async def test_unlink_clears_both_credentials_and_choice(self, ctx, creds):
        await ctx.albums.save_link(creds)
        await ctx.albums.choose_album("album-1", "Family")
        await ctx.albums.unlink()
        settings = await ctx.settings.get()
        assert settings.album_credentials_enc is None
        assert settings.album_bundle_id is None
        assert settings.album_enabled is False


class TestUploaderHook:
    async def test_add_to_album_is_only_attempted_for_photos_and_videos(self):
        from pathlib import Path

        from fotohu.worker.uploader import _is_photo_or_video

        assert _is_photo_or_video(Path("IMG_0001.JPG"))
        assert _is_photo_or_video(Path("clip.mp4"))
        assert _is_photo_or_video(Path("photo.heic"))
        assert not _is_photo_or_video(Path("document.pdf"))
