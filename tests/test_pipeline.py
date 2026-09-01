"""End-to-end: a message arrives, the bytes reach storage unchanged, the chat clears."""

from __future__ import annotations

import hashlib
from datetime import datetime

from fotohu.core.models import IncomingMedia, Platform, Role, SourceKind
from fotohu.worker.uploader import UploadWorker


async def make_storage(ctx, tmp_path):
    account_id = await ctx.repo.create_storage_account(
        backend="local", label="Test", root_folder="FotoHu",
        extra={"base_path": str(tmp_path / "cloud")},
    )
    await ctx.repo.set_default_storage(account_id)
    return account_id


async def make_person(ctx, name="Дмитрий", role=Role.ADMIN, uid="42"):
    person = await ctx.repo.create_person(name=name, role=role)
    await ctx.repo.link_account(person.id, Platform.TELEGRAM, uid, name, "100")
    return person


def media(ref, name="IMG_0042.JPG", kind=SourceKind.DOCUMENT, message_id="5", **kwargs):
    return IncomingMedia(
        platform=Platform.TELEGRAM,
        chat_id="100",
        message_id=message_id,
        source_kind=kind,
        file_name=name,
        file_ref=ref,
        sent_at=datetime(2026, 3, 14, 9, 26, 53),
        **kwargs,
    )


def worker(ctx, adapter):
    ctx.adapters[Platform.TELEGRAM] = adapter
    return UploadWorker(
        repo=ctx.repo,
        settings_service=ctx.settings,
        registry=ctx.storage,
        adapters=ctx.adapters,
        temp_dir=ctx.config.temp_dir,
    )


async def drain(uploader) -> None:
    """Process the whole queue synchronously, without starting background tasks."""
    while (record := await uploader.repo.claim_next_upload()) is not None:
        await uploader._handle(record)


class TestLosslessUpload:
    async def test_document_reaches_storage_byte_identical(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["state"] == "done"
        assert row["remote_path"] == "FotoHu/dmitrii/2026/2026-03/IMG_0042.JPG"
        assert row["verified"] == 1

        stored = tmp_path / "cloud" / row["remote_path"]
        # The actual requirement: the bytes in the cloud are the bytes sent.
        assert stored.read_bytes() == jpeg_bytes
        assert row["sha256"] == hashlib.sha256(jpeg_bytes).hexdigest()

    async def test_exif_capture_date_decides_the_folder(
        self, ctx, adapter, tmp_path, jpeg_with_exif
    ):
        await make_storage(ctx, tmp_path)
        person = await make_person(ctx)
        # Shot in 2018, but forwarded to the bot in 2026: it must file by capture date.
        payload = jpeg_with_exif("2018:01:02 03:04:05")
        adapter.put("ref-1", payload)
        await ctx.repo.create_upload(media("ref-1"), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["date_source"] == "exif"
        assert row["remote_path"] == "FotoHu/dmitrii/2018/2018-01/IMG_0042.JPG"
        # Reading EXIF must never rewrite the file.
        assert (tmp_path / "cloud" / row["remote_path"]).read_bytes() == payload

    async def test_exif_can_be_switched_off_in_favour_of_the_message_date(
        self, ctx, adapter, tmp_path, jpeg_with_exif
    ):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("prefer_exif_date", False)
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_with_exif("2018:01:02 03:04:05"))
        await ctx.repo.create_upload(media("ref-1"), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["date_source"] == "message"
        assert "2026/2026-03" in row["remote_path"]

    async def test_message_date_is_used_when_there_is_no_exif(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["date_source"] == "message"
        assert "2026/2026-03" in row["remote_path"]


class TestQualityPolicy:
    async def test_compressed_photos_are_rejected_by_default(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        person = await make_person(ctx)
        adapter.put("ref-p", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-p", kind=SourceKind.PHOTO), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["state"] == "rejected"
        assert not (tmp_path / "cloud").exists() or not any(
            (tmp_path / "cloud").rglob("*.jpg")
        )
        assert any("файлом" in text for _, text in adapter.sent)

    async def test_save_marked_puts_them_in_a_separate_folder(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("photo_policy", "save_marked")
        person = await make_person(ctx)
        adapter.put("ref-p", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-p", kind=SourceKind.PHOTO), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["state"] == "done"
        assert "_compressed" in row["remote_path"]


class TestDeduplication:
    async def test_identical_bytes_are_not_uploaded_twice(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_bytes)
        adapter.put("ref-2", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1", message_id="5"), person.id)
        await ctx.repo.create_upload(media("ref-2", message_id="6"), person.id)

        await drain(worker(ctx, adapter))

        rows = sorted(await ctx.repo.recent_uploads(), key=lambda r: r["message_id"])
        assert rows[0]["state"] == "done"
        assert rows[1]["state"] == "skipped_dup"
        assert len(list((tmp_path / "cloud").rglob("IMG_0042*.JPG"))) == 1

    async def test_different_files_with_the_same_name_both_survive(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_bytes)
        adapter.put("ref-2", jpeg_bytes + b"\x00different")
        await ctx.repo.create_upload(media("ref-1", message_id="5"), person.id)
        await ctx.repo.create_upload(media("ref-2", message_id="6"), person.id)

        await drain(worker(ctx, adapter))

        names = sorted(p.name for p in (tmp_path / "cloud").rglob("IMG_0042*.JPG"))
        # Losing one of two distinct photos would be the worst possible bug.
        assert names == ["IMG_0042 (2).JPG", "IMG_0042.JPG"]

    async def test_the_same_telegram_message_is_never_queued_twice(self, ctx):
        person = await make_person(ctx)
        first = await ctx.repo.create_upload(media("ref-1"), person.id)
        second = await ctx.repo.create_upload(media("ref-1"), person.id)
        assert first is not None
        assert second is None


class TestFolderModes:
    async def test_two_people_get_separate_folders(self, ctx, adapter, tmp_path, jpeg_bytes):
        await make_storage(ctx, tmp_path)
        dad = await make_person(ctx, "Папа", uid="1")
        mum = await make_person(ctx, "Мама", role=Role.MEMBER, uid="2")
        adapter.put("a", jpeg_bytes)
        adapter.put("b", jpeg_bytes + b"x")
        await ctx.repo.create_upload(media("a", message_id="10"), dad.id)
        await ctx.repo.create_upload(media("b", message_id="11"), mum.id)

        await drain(worker(ctx, adapter))

        paths = {r["remote_path"] for r in await ctx.repo.recent_uploads()}
        assert any("/papa/" in p for p in paths)
        assert any("/mama/" in p for p in paths)

    async def test_a_group_puts_selected_members_in_one_folder(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("folder_mode", "per_group")
        group = await ctx.repo.create_group("Родители", "parents")

        dad = await make_person(ctx, "Папа", uid="1")
        mum = await make_person(ctx, "Мама", role=Role.MEMBER, uid="2")
        await ctx.repo.update_person(dad.id, group_id=group.id)
        await ctx.repo.update_person(mum.id, group_id=group.id)

        adapter.put("a", jpeg_bytes)
        adapter.put("b", jpeg_bytes + b"x")
        await ctx.repo.create_upload(media("a", message_id="10"), dad.id)
        await ctx.repo.create_upload(media("b", message_id="11"), mum.id)

        await drain(worker(ctx, adapter))

        paths = {r["remote_path"] for r in await ctx.repo.recent_uploads()}
        assert all("/parents/" in p for p in paths), paths


class TestFailureHandling:
    async def test_nothing_is_uploaded_without_a_configured_cloud(
        self, ctx, adapter, jpeg_bytes
    ):
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), person.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["state"] == "failed"
        assert "storage" in (row["last_error"] or "")

    async def test_a_failed_upload_is_retried_later(self, ctx, adapter, tmp_path, jpeg_bytes):
        person = await make_person(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), person.id)
        await drain(worker(ctx, adapter))

        # Backing off means it is not immediately claimable again.
        assert await ctx.repo.claim_next_upload() is None

        await make_storage(ctx, tmp_path)
        await ctx.repo.reset_failed()
        await drain(worker(ctx, adapter))
        assert (await ctx.repo.recent_uploads())[0]["state"] == "done"
