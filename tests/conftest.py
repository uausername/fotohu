"""Shared fixtures: an in-memory-ish app context wired to fakes, not the network."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fotohu.config import Config, TelegramConfig, ViberConfig
from fotohu.context import AppContext
from fotohu.core.download import stream_to_file
from fotohu.core.models import LocalFile, Platform
from fotohu.db import connect, migrate
from fotohu.db.repo import Repo
from fotohu.messengers.base import DeleteResult, MessengerAdapter
from fotohu.services.members import MemberService
from fotohu.services.settings import SettingsService
from fotohu.storage.registry import StorageRegistry

SECRET = "test-secret-key-not-a-real-fernet-key"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        temp_dir=tmp_path / "tmp",
        secret_key=SECRET,
        public_url="https://example.test",
        bootstrap_token="BOOTSTRAP",
        log_level="WARNING",
        language="ru",
        http_host="127.0.0.1",
        http_port=8080,
        telegram=TelegramConfig(token="123456:TESTTOKEN"),
        viber=ViberConfig(token="abc-def-ghi"),
    )


@pytest.fixture
async def ctx(config: Config):
    conn = await connect(config.db_path)
    await migrate(conn)
    repo = Repo(conn)
    settings = SettingsService(repo)
    context = AppContext(
        config=config,
        conn=conn,
        repo=repo,
        settings=settings,
        members=MemberService(repo, settings),
        storage=StorageRegistry(repo, config.secret_key),
    )
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    yield context
    await conn.close()


class FakeAdapter(MessengerAdapter):
    """Stands in for Telegram: serves bytes from a dict, records what it sends."""

    def __init__(
        self,
        platform: Platform = Platform.TELEGRAM,
        supports_deletion: bool = True,
        delete_window_hours: int | None = 48,
    ) -> None:
        self.platform = platform
        self.supports_deletion = supports_deletion
        self.delete_window_hours = delete_window_hours
        self.download_limit = None
        self.files: dict[str, bytes] = {}
        self.sent: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, list[str]]] = []
        #: message ids the fake should refuse to delete, and why
        self.undeletable: dict[str, str] = {}
        self._next_message_id = 9000

    def put(self, ref: str, payload: bytes) -> str:
        self.files[ref] = payload
        return ref

    async def download(self, file_ref: str, dest: Path, size_limit=None) -> LocalFile:
        payload = self.files[file_ref]

        async def chunks():
            for i in range(0, len(payload), 1024):
                yield payload[i : i + 1024]

        return await stream_to_file(chunks(), dest, size_limit=size_limit)

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None):
        self.sent.append((chat_id, text))
        self._next_message_id += 1
        return str(self._next_message_id)

    async def delete_messages(self, chat_id: str, message_ids: list[str]) -> DeleteResult:
        self.deleted.append((chat_id, list(message_ids)))
        result = DeleteResult()
        for mid in message_ids:
            if mid in self.undeletable:
                result.failed[mid] = self.undeletable[mid]
            else:
                result.deleted.append(mid)
        return result


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def jpeg_bytes() -> bytes:
    """A tiny but genuinely valid JPEG, so Pillow's EXIF path is really exercised."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (120, 30, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def jpeg_with_exif():
    """Build a JPEG carrying a real DateTimeOriginal, for the date-routing tests."""

    def _make(taken: str = "2018:01:02 03:04:05", colour=(10, 20, 30)) -> bytes:
        from io import BytesIO

        from PIL import Image

        image = Image.new("RGB", (8, 8), colour)
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = taken  # DateTimeOriginal
        buffer = BytesIO()
        image.save(buffer, format="JPEG", exif=exif)
        return buffer.getvalue()

    return _make


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
