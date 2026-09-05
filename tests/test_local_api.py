"""The self-hosted Bot API server hands us a path, not bytes — and never tidies up.

Everything here is about that one difference. With the public API the file
arrives over HTTP and Telegram owns the copy; with `telegram-bot-api` the file
is already on a disk we share, and once we have taken our own copy nobody else
is ever going to remove theirs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fotohu.core.errors import DownloadError
from fotohu.messengers.telegram.adapter import TelegramAdapter


class FakeFile:
    def __init__(self, path: str, size: int) -> None:
        self.file_path = path
        self.file_size = size


class FakeBot:
    """Just enough Bot for `download`: it answers getFile with a local path."""

    def __init__(self, file: FakeFile) -> None:
        self.file = file
        self.download_calls = 0

    async def get_file(self, file_ref: str) -> FakeFile:
        return self.file

    async def download_file(self, path: str, chunk_size: int = 0):
        self.download_calls += 1
        raise AssertionError("local mode must not stream; it copies from disk")


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    """Stand-in for /var/lib/telegram-bot-api, written by the other container."""
    directory = tmp_path / "spool" / "videos"
    directory.mkdir(parents=True)
    return directory


class TestLocalSpoolIsReclaimed:
    async def test_the_servers_copy_is_removed_once_ours_exists(self, spool, tmp_path):
        source = spool / "big.mp4"
        source.write_bytes(b"video bytes")
        adapter = TelegramAdapter(FakeBot(FakeFile(str(source), 11)), local_mode=True)

        local = await adapter.download("file-id", tmp_path / "upload-1")

        assert local.path.read_bytes() == b"video bytes"
        assert local.size == 11
        assert not source.exists(), "the spool copy outlived the download"

    async def test_a_refused_unlink_does_not_lose_the_download(
        self, spool, tmp_path, monkeypatch, caplog
    ):
        """Under Docker the unlink is always refused, and that must cost us nothing.

        The server owns the spool as uid 101 and writes it 0750: the bot is in
        that group so it can read, but unlinking needs write on the directory,
        which it does not have. The `tg-api-gc` sidecar sweeps as root instead.
        Losing the bytes here would be far worse than the disk left behind — the
        upload would be retried from scratch for no reason — and a warning on
        every single video would train everyone to ignore the log.
        """
        caplog.set_level(logging.DEBUG)
        source = spool / "big.mp4"
        source.write_bytes(b"video bytes")

        def refuse(self: Path) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "unlink", refuse)
        adapter = TelegramAdapter(FakeBot(FakeFile(str(source), 11)), local_mode=True)

        local = await adapter.download("file-id", tmp_path / "upload-1")

        assert local.path.read_bytes() == b"video bytes"
        assert "leaving" in caplog.text
        assert "WARNING" not in caplog.text

    async def test_an_unexpected_failure_is_still_worth_a_warning(
        self, spool, tmp_path, monkeypatch, caplog
    ):
        """A refusal is the designed-for case; anything else is not, and should say so."""
        source = spool / "big.mp4"
        source.write_bytes(b"video bytes")

        def fail(self: Path) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(Path, "unlink", fail)
        adapter = TelegramAdapter(FakeBot(FakeFile(str(source), 11)), local_mode=True)

        local = await adapter.download("file-id", tmp_path / "upload-1")

        assert local.path.read_bytes() == b"video bytes"
        assert "could not reclaim" in caplog.text

    async def test_a_vanished_path_is_a_download_error_not_a_stream(self, spool, tmp_path):
        """Silence here used to mean a FileNotFoundError from deep inside aiogram.

        With `is_local` there is no HTTP route to fall back to — aiogram reads
        the same path off the disk — so the honest answer is a DownloadError
        naming the likely cause, which the worker retries with backoff.
        """
        bot = FakeBot(FakeFile(str(spool / "gone.mp4"), 11))
        adapter = TelegramAdapter(bot, local_mode=True)

        with pytest.raises(DownloadError, match="share the same volume"):
            await adapter.download("file-id", tmp_path / "upload-1")

        assert bot.download_calls == 0


class TestPublicApiIsUntouched:
    async def test_relative_paths_still_stream(self, tmp_path):
        """The public API returns `videos/file_1.mp4`, and nothing on our disk."""
        bot = FakeBot(FakeFile("videos/file_1.mp4", 11))
        adapter = TelegramAdapter(bot, local_mode=False)

        with pytest.raises(AssertionError, match="local mode must not stream"):
            await adapter.download("file-id", tmp_path / "upload-1")
