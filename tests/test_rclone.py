"""The rclone backend: one listing per directory, and a copy that verifies itself."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from fotohu.core.errors import StorageError
from fotohu.core.models import LocalFile
from fotohu.storage.rclone_backend import RcloneBackend
from fotohu.storage.rclone_daemon import RcloneDaemon, RcloneRemoteError

DIR = "FotoHu/dima/2026/2026-09"


def entry(name: str, size: int = 10, **hashes: str) -> dict:
    return {
        "Path": f"{DIR}/{name}",
        "Name": name,
        "Size": size,
        "IsDir": False,
        "ID": f"id-{name}",
        "Hashes": dict(hashes),
    }


class FakeDaemon:
    """Answers RC calls from a script, and remembers what it was asked."""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, path: str, payload: dict, *, timeout: int = 0) -> dict:
        self.calls.append((path, payload))
        if path in self.errors:
            raise self.errors[path]
        answer = self.responses.get(path, {})
        return answer(payload) if callable(answer) else answer

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def make_backend(daemon, remote: str = "onedrive") -> RcloneBackend:
    return RcloneBackend(
        account_id=1, root_folder="FotoHu", credentials={},
        extra={"remote": remote}, daemon=daemon,
    )


def local_file(tmp_path: Path, payload: bytes = b"photo bytes") -> LocalFile:
    path = tmp_path / "upload-1-IMG_0042.JPG"
    path.write_bytes(payload)
    return LocalFile(
        path=path,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload).hexdigest(),
        sha1=hashlib.sha1(payload).hexdigest(),
    )


class TestDirectoryListing:
    async def test_a_directory_is_listed_once_however_many_names_we_try(self):
        daemon = FakeDaemon({"operations/list": {"list": [entry("IMG_0042.JPG")]}})
        backend = make_backend(daemon)

        assert await backend.exists(DIR, "IMG_0042.JPG") is not None
        assert await backend.exists(DIR, "IMG_0042 (2).JPG") is None
        assert await backend.exists(DIR, "IMG_0042 (3).JPG") is None

        assert daemon.paths == ["operations/list"]

    async def test_a_month_folder_that_does_not_exist_yet_reads_as_empty(self):
        daemon = FakeDaemon()
        daemon.errors["operations/list"] = RcloneRemoteError("directory not found", 404)
        backend = make_backend(daemon)

        assert await backend.exists(DIR, "IMG_0042.JPG") is None

    async def test_a_real_failure_is_not_mistaken_for_an_empty_folder(self):
        daemon = FakeDaemon()
        daemon.errors["operations/list"] = RcloneRemoteError("backend exploded", 500)
        backend = make_backend(daemon)

        with pytest.raises(StorageError):
            await backend.exists(DIR, "IMG_0042.JPG")

    async def test_a_hash_the_provider_reports_is_carried_through(self):
        daemon = FakeDaemon(
            {"operations/list": {"list": [entry("IMG_0042.JPG", quickxor="abc123")]}}
        )
        backend = make_backend(daemon)

        found = await backend.exists(DIR, "IMG_0042.JPG")

        assert found is not None
        assert found.hashes == {"quickxor": "abc123"}


class TestUpload:
    def _daemon(self, **hashes: str) -> FakeDaemon:
        return FakeDaemon({
            "operations/list": {"list": []},
            "operations/copyfile": {},
            "operations/stat": {"item": entry("IMG_0042.JPG", size=11, **hashes)},
        })

    async def test_a_copy_the_provider_hashed_counts_as_verified(self, tmp_path):
        daemon = self._daemon(quickxor="abc123")
        backend = make_backend(daemon)

        stored = await backend.upload(local_file(tmp_path), DIR, "IMG_0042.JPG")

        assert stored.verified_on_write is True
        assert stored.path == f"{DIR}/IMG_0042.JPG"
        assert daemon.paths == ["operations/copyfile", "operations/stat"]

    async def test_a_provider_that_reports_no_hash_claims_nothing(self, tmp_path):
        backend = make_backend(self._daemon())

        stored = await backend.upload(local_file(tmp_path), DIR, "IMG_0042.JPG")

        assert stored.verified_on_write is False

    async def test_the_copy_is_told_where_the_local_file_is(self, tmp_path):
        daemon = self._daemon(quickxor="abc")
        backend = make_backend(daemon)
        local = local_file(tmp_path)

        await backend.upload(local, DIR, "IMG_0042.JPG")

        _, payload = daemon.calls[0]
        assert payload["srcFs"] == str(tmp_path)
        assert payload["srcRemote"] == local.path.name
        assert payload["dstFs"] == "onedrive:"
        assert payload["dstRemote"] == f"{DIR}/IMG_0042.JPG"

    async def test_a_vanished_upload_is_an_error_not_a_success(self, tmp_path):
        daemon = self._daemon()
        daemon.responses["operations/stat"] = {"item": None}
        backend = make_backend(daemon)

        with pytest.raises(StorageError, match="not there"):
            await backend.upload(local_file(tmp_path), DIR, "IMG_0042.JPG")

    async def test_the_cached_listing_is_dropped_after_we_change_the_folder(self, tmp_path):
        daemon = self._daemon(quickxor="abc")
        backend = make_backend(daemon)

        await backend.exists(DIR, "IMG_0042.JPG")
        await backend.upload(local_file(tmp_path), DIR, "IMG_0042.JPG")
        await backend.exists(DIR, "IMG_0042.JPG")

        assert daemon.paths.count("operations/list") == 2


# --------------------------------------------------------------------- live rclone


def find_rclone() -> str | None:
    """The real binary, if this machine has one. CI usually does not."""
    candidates = [os.environ.get("RCLONE_BINARY"), shutil.which("rclone")]
    for name in ("bin/rclone.exe", "bin/rclone"):
        local = Path(__file__).resolve().parent.parent / name
        if local.is_file():
            candidates.append(str(local))
    return next((c for c in candidates if c and (shutil.which(c) or Path(c).is_file())), None)


RCLONE = find_rclone()


@pytest.mark.skipif(RCLONE is None, reason="no rclone binary on this machine")
class TestAgainstRealRclone:
    """Drives a real daemon against a local alias remote — no network, no cloud."""

    @pytest.fixture
    async def backend(self, tmp_path):
        cloud = tmp_path / "cloud"
        cloud.mkdir()
        config = tmp_path / "rclone.conf"
        config.write_text(f"[testlocal]\ntype = alias\nremote = {cloud}\n", encoding="utf-8")

        daemon = RcloneDaemon(RCLONE, str(config))
        backend = RcloneBackend(
            account_id=1, root_folder="FotoHu", credentials={},
            extra={"remote": "testlocal"}, daemon=daemon,
        )
        yield backend
        await daemon.stop()

    async def test_a_file_goes_up_verified_and_can_be_found_again(self, backend, tmp_path):
        local = local_file(tmp_path, b"the actual photo bytes")

        stored = await backend.upload(local, DIR, "IMG_0042.JPG")

        # The point of the exercise: rclone really does report a digest we can
        # record, so the verified flag means something.
        assert stored.hashes, "rclone returned no hashes — showHash is not working"
        assert stored.verified_on_write is True
        assert backend.verify(local, stored) is True
        assert stored.size == local.size

        found = await backend.exists(DIR, "IMG_0042.JPG")
        assert found is not None
        assert found.hashes == stored.hashes

    async def test_an_empty_folder_and_a_missing_one_both_read_as_empty(self, backend):
        assert await backend.exists("FotoHu/nothing/here", "IMG_0042.JPG") is None
        await backend.ensure_folder("FotoHu/nothing/here")
        assert await backend.exists("FotoHu/nothing/here", "IMG_0042.JPG") is None

    async def test_the_daemon_comes_back_after_it_dies(self, backend, tmp_path):
        await backend.check()
        await backend.daemon.stop()

        # The next call must notice and start a new one rather than fail.
        stored = await backend.upload(local_file(tmp_path), DIR, "IMG_0043.JPG")
        assert stored.verified_on_write is True
