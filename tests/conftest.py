"""Shared fixtures: an in-memory-ish app context wired to fakes, not the network."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fotohu.config import Config, TelegramConfig, ViberConfig
from fotohu.context import AppContext
from fotohu.core.download import stream_to_file
from fotohu.core.errors import RetryableError
from fotohu.core.models import LocalFile, Platform
from fotohu.db import connect, migrate
from fotohu.db.repo import Repo
from fotohu.messengers.base import DeleteResult, MessengerAdapter, SentPhoto
from fotohu.services.albums import AlbumService
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
        log_file=None,
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
        storage=StorageRegistry(
            repo,
            config.secret_key,
            rclone_binary=config.rclone_binary,
            rclone_config=config.rclone_config,
        ),
        albums=AlbumService(settings, config.secret_key),
    )
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    yield context
    await conn.close()
    await context.albums.close()


class FakeAdapter(MessengerAdapter):
    """Stands in for Telegram: serves bytes from a dict, records what it sends."""

    def __init__(
        self,
        platform: Platform = Platform.TELEGRAM,
        supports_deletion: bool = True,
        delete_window_hours: int | None = 48,
        supports_photo: bool = True,
    ) -> None:
        self.platform = platform
        self.supports_deletion = supports_deletion
        self.delete_window_hours = delete_window_hours
        self.supports_photo = supports_photo
        self.download_limit = None
        self.files: dict[str, bytes] = {}
        self.sent: list[tuple[str, str]] = []
        #: (chat, what was handed over, caption) per send_photo call. A Path means
        #: the bytes went over the wire; a str means a handle was reused.
        self.photos: list[tuple[str, Path | str, str | None]] = []
        self.deleted: list[tuple[str, list[str]]] = []
        #: message ids the fake should refuse to delete, and why
        self.undeletable: dict[str, str] = {}
        #: bytes of every picture actually uploaded, kept because the sender
        #: deletes its temp file as soon as the last recipient has it
        self.photo_uploads: list[bytes] = []
        #: chats where send_photo should blow up, and why
        self.photo_errors: dict[str, str] = {}
        #: chats whose *first* send_photo is refused by flood control, and the
        #: pause Telegram asks for in return
        self.flood_once: dict[str, float] = {}
        self._next_message_id = 9000
        self._next_file_id = 0

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

    async def send_photo(self, chat_id: str, photo, caption: str | None = None):
        if chat_id in self.flood_once:
            delay = self.flood_once.pop(chat_id)
            raise RetryableError(f"flood control: retry after {delay}s", delay)
        if chat_id in self.photo_errors:
            raise RuntimeError(self.photo_errors[chat_id])
        self.photos.append((chat_id, photo, caption))
        self._next_message_id += 1
        if isinstance(photo, Path):
            # Real bytes: Telegram would hand back a handle for re-sending.
            self.photo_uploads.append(photo.read_bytes())
            self._next_file_id += 1
            ref = f"fake-file-{self._next_file_id}"
        else:
            ref = photo
        return SentPhoto(message_id=str(self._next_message_id), reusable_ref=ref)

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


@pytest.fixture
def heic_with_exif():
    """An iPhone photo as it really arrives: HEIC, sent as a file, with EXIF.

    Pillow cannot read this format on its own — that is the whole point of the
    fixture. Everything downstream of it (capture date, preview, feed) works
    only because pillow-heif is a declared dependency.
    """

    def _make(taken: str = "2019:07:14 18:30:00", size=(1024, 768)) -> bytes:
        from io import BytesIO

        import pillow_heif
        from PIL import Image

        image = Image.new("RGB", size, (90, 140, 210))
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = taken  # DateTimeOriginal
        buffer = BytesIO()
        # Written through pillow-heif's own encoder on purpose: registering the
        # opener here would hand Pillow the very ability the code under test is
        # supposed to provide, and the test would pass with the fix removed.
        pillow_heif.from_pillow(image).save(buffer, quality=60, exif=exif.tobytes())
        return buffer.getvalue()

    return _make


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
