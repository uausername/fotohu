"""What broke the first time a working install was moved to a server.

Both cases below passed every test and worked perfectly on the machine they were
set up on, and both failed the moment the same database ran somewhere else.
"""

from __future__ import annotations

import json
from dataclasses import replace

from fotohu.config import ViberConfig, load_config
from fotohu.storage.registry import StorageRegistry
from fotohu.web.app import build_app


def _rclone_record(extra: dict) -> dict:
    return {
        "id": 1,
        "backend": "rclone",
        "root_folder": "FotoHu",
        "credentials_enc": None,
        "extra_json": json.dumps(extra),
    }


class TestRcloneHostPaths:
    """Where rclone lives belongs to the machine, not to the linked account.

    The admin flow used to snapshot ``RCLONE_BINARY`` and ``RCLONE_CONFIG`` into
    the account row when the remote was linked. Carrying that database to a
    server meant every upload died with "rclone binary 'C:\\...\\rclone.exe' not
    found on PATH" — with the storage still reporting itself as configured.
    """

    async def test_stale_paths_in_the_row_are_ignored(self, ctx):
        registry = StorageRegistry(
            ctx.repo,
            ctx.config.secret_key,
            rclone_binary="rclone",
            rclone_config="/data/rclone.conf",
        )
        backend = await registry.build(
            _rclone_record(
                {
                    "remote": "onedrive",
                    # exactly what a Windows install wrote before migrating
                    "binary": r"C:\mycode\fotohu\bin\rclone.exe",
                    "config_path": r"C:\mycode\fotohu\data\rclone.conf",
                }
            )
        )

        assert backend.binary == "rclone"
        assert backend.config_path == "/data/rclone.conf"

    async def test_the_remote_name_is_still_taken_from_the_row(self, ctx):
        """It is the one part of the config that really is per-account."""
        registry = StorageRegistry(ctx.repo, ctx.config.secret_key)
        backend = await registry.build(_rclone_record({"remote": "onedrive"}))

        assert backend.remote == "onedrive:"
        assert backend.binary == "rclone"
        assert backend.config_path is None


class TestHealthEndpoint:
    """``/health`` has to answer in every configuration, or it cannot be probed.

    The HTTP server used to start only when Viber, a Telegram webhook or a public
    URL was configured. The documented Telegram-only setup has none of those, so
    the container healthcheck could never succeed and the deployment sat there
    reporting "unhealthy" while archiving photos perfectly well.
    """

    async def test_routed_without_viber_or_a_public_url(self, ctx):
        ctx.config = replace(ctx.config, viber=ViberConfig(), public_url=None)
        app = build_app(ctx)

        paths = {route.resource.canonical for route in app.router.routes()}
        assert "/health" in paths
        assert ctx.config.viber_webhook_path not in paths

    async def test_public_access_flag_tracks_only_inbound_features(self, ctx):
        telegram_only = replace(ctx.config, viber=ViberConfig(), public_url=None)
        assert telegram_only.needs_public_access is False

        with_viber = replace(telegram_only, viber=ViberConfig(token="abc"))
        assert with_viber.needs_public_access is True


class TestBindAddress:
    """A bare-metal install should not open a port on the LAN by default."""

    def test_defaults_to_loopback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOTOHU_SECRET_KEY", "x" * 32)
        monkeypatch.delenv("FOTOHU_HTTP_HOST", raising=False)
        config = load_config(tmp_path / "absent.env")

        assert config.http_host == "127.0.0.1"

    def test_environment_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FOTOHU_SECRET_KEY", "x" * 32)
        monkeypatch.setenv("FOTOHU_HTTP_HOST", "0.0.0.0")
        config = load_config(tmp_path / "absent.env")

        assert config.http_host == "0.0.0.0"
