"""The shared feed: everyone sees the photo, the archive still holds one copy."""

from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from conftest import FakeAdapter
from PIL import Image
from test_pipeline import drain, make_storage, media, worker

from fotohu.core.models import Platform, Role, SourceKind
from fotohu.core.preview import make_preview
from fotohu.messengers.telegram import handlers
from fotohu.messengers.telegram.adapter import CAPTION_LIMIT, TelegramAdapter
from fotohu.worker.purger import PurgeWorker


async def family(ctx, *, chats=("100", "200", "300")):
    """A sender plus the relatives who should see what they send."""
    people = []
    for n, chat in enumerate(chats):
        person = await ctx.repo.create_person(
            name=f"Человек-{n}", role=Role.ADMIN if n == 0 else Role.MEMBER
        )
        await ctx.repo.link_account(
            person.id, Platform.TELEGRAM, str(40 + n), person.name, chat
        )
        people.append(person)
    return people


def photos_in(adapter: FakeAdapter, chat_id: str) -> list:
    return [row for row in adapter.photos if row[0] == chat_id]


class TestTheFeed:
    async def test_a_saved_photo_reaches_the_other_family_chats(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        assert [row[0] for row in adapter.photos] == ["200", "300"]

    async def test_the_sender_is_not_shown_their_own_photo(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        # They are looking at their own message; a second copy is just noise.
        assert photos_in(adapter, "100") == []

    async def test_it_arrives_as_a_picture_not_as_a_file(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        # The whole point of the feature: a photo, decodable as one.
        assert adapter.photo_uploads
        with Image.open(BytesIO(adapter.photo_uploads[0])) as preview:
            assert preview.format == "JPEG"

    async def test_the_caption_says_who_sent_it(self, ctx, adapter, tmp_path, jpeg_bytes):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1", caption="дача, май"), sender.id)

        await drain(worker(ctx, adapter))

        _, _, caption = adapter.photos[0]
        assert sender.name in caption
        assert "дача, май" in caption

    async def test_the_bytes_are_uploaded_once_however_many_relatives_there_are(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200", "300", "400"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        # One send carries the picture; the rest ride the handle it came back with.
        assert len(adapter.photo_uploads) == 1
        assert [isinstance(row[1], Path) for row in adapter.photos] == [True, False, False]

    async def test_a_blocked_member_is_shown_nothing(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, other, _third = await family(ctx)
        await ctx.repo.update_person(other.id, status="blocked")
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        assert [row[0] for row in adapter.photos] == ["300"]

    async def test_the_feed_can_be_switched_off(self, ctx, adapter, tmp_path, jpeg_bytes):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("mirror_enabled", False)
        sender, *_ = await family(ctx)
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        assert adapter.photos == []
        assert (await ctx.repo.recent_uploads())[0]["state"] == "done"

    async def test_anything_that_is_not_a_picture_is_simply_not_shown(
        self, ctx, adapter, tmp_path
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx)
        adapter.put("ref-1", b"MOV\x00 not an image at all")
        await ctx.repo.create_upload(media("ref-1", name="clip.mov"), sender.id)

        await drain(worker(ctx, adapter))

        assert adapter.photos == []
        # ...and the archive took it anyway: the feed is a bonus, not a gate.
        assert (await ctx.repo.recent_uploads())[0]["state"] == "done"

    async def test_a_messenger_that_cannot_post_pictures_is_skipped(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, other, _ = await family(ctx)
        viber = FakeAdapter(
            platform=Platform.VIBER, supports_deletion=False,
            delete_window_hours=None, supports_photo=False,
        )
        await ctx.repo.link_account(other.id, Platform.VIBER, "v-1", other.name, "v-chat")
        ctx.adapters[Platform.VIBER] = viber
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        assert viber.photos == []
        assert [row[0] for row in adapter.photos] == ["200", "300"]

    async def test_a_chat_that_refuses_the_photo_costs_only_that_chat(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx)
        adapter.photo_errors["200"] = "bot was blocked by the user"
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert row["state"] == "done"  # the archive is what matters
        assert [r[0] for r in adapter.photos] == ["300"]
        assert [m["chat_id"] for m in await ctx.repo.list_mirrors(row["id"])] == ["300"]


class TestItNeverArchivesTwice:
    async def test_showing_a_photo_to_five_people_still_means_one_upload(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200", "300", "400", "500", "600"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        cur = await ctx.conn.execute("SELECT COUNT(*) AS n FROM uploads")
        assert (await cur.fetchone())["n"] == 1
        assert len(list((tmp_path / "cloud").rglob("*.JPG"))) == 1
        assert len(adapter.photos) == 5

    async def test_a_repeated_fan_out_does_not_show_the_same_photo_twice(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)
        uploader = worker(ctx, adapter)
        await drain(uploader)

        row = (await ctx.repo.recent_uploads())[0]
        source = ctx.config.temp_dir / "again.jpg"
        source.write_bytes(jpeg_bytes)
        shown = await uploader.mirror.fan_out(
            record=row,
            person=sender,
            settings=await ctx.settings.get(),
            source=source,
            purge_after=None,
        )

        assert shown == 0
        assert len(adapter.photos) == 1
        assert len(await ctx.repo.list_mirrors(row["id"])) == 1

    async def test_a_feed_photo_sent_back_to_the_bot_is_not_queued(self):
        ours = SimpleNamespace(id=777)
        mine = SimpleNamespace(
            bot=SimpleNamespace(id=777),
            forward_origin=SimpleNamespace(sender_user=SimpleNamespace(id=777)),
        )
        theirs = SimpleNamespace(
            bot=SimpleNamespace(id=777),
            forward_origin=SimpleNamespace(sender_user=SimpleNamespace(id=5)),
        )
        fresh = SimpleNamespace(bot=SimpleNamespace(id=777), forward_origin=None)

        assert handlers._is_our_own_photo(mine) is True
        assert handlers._is_our_own_photo(theirs) is False
        assert handlers._is_our_own_photo(fresh) is False
        assert ours.id == 777  # the bot id is what the check hinges on

    async def test_the_pre_bot_api_7_forward_field_is_understood_too(self):
        legacy = SimpleNamespace(
            bot=SimpleNamespace(id=777),
            forward_origin=None,
            forward_from=SimpleNamespace(id=777),
        )
        assert handlers._is_our_own_photo(legacy) is True


class TestItLeavesWithTheOriginal:
    async def test_a_mirror_gets_the_same_deadline_as_the_upload(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)

        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        mirror = (await ctx.repo.list_mirrors(row["id"]))[0]
        upload_deadline = datetime.strptime(row["purge_after"], "%Y-%m-%d %H:%M:%S")
        mirror_deadline = datetime.strptime(mirror["purge_after"], "%Y-%m-%d %H:%M:%S")
        assert abs(mirror_deadline - upload_deadline) < timedelta(seconds=5)

    async def test_the_same_sweep_takes_the_mirror_away(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("purge_after_hours", 0)  # due the moment it lands
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)
        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        mirror = (await ctx.repo.list_mirrors(row["id"]))[0]
        removed = await PurgeWorker(ctx.repo, ctx.settings, ctx.adapters).sweep()

        assert removed == 2  # the original in one chat, the mirror in the other
        assert ("200", [mirror["message_id"]]) in adapter.deleted
        assert (await ctx.repo.list_mirrors(row["id"]))[0]["purged_at"] is not None
        assert (await ctx.repo.recent_uploads())[0]["purged_at"] is not None

    async def test_purging_off_keeps_the_feed_as_well(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("purge_enabled", False)
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)
        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        assert (await ctx.repo.list_mirrors(row["id"]))[0]["purge_after"] is None
        assert await PurgeWorker(ctx.repo, ctx.settings, ctx.adapters).sweep() == 0

    async def test_a_mirror_past_the_telegram_window_is_recorded_not_retried(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        await ctx.settings.set("purge_after_hours", 0)
        sender, *_ = await family(ctx, chats=("100", "200"))
        adapter.put("ref-1", jpeg_bytes)
        await ctx.repo.create_upload(media("ref-1"), sender.id)
        await drain(worker(ctx, adapter))

        row = (await ctx.repo.recent_uploads())[0]
        # Pretend the bot posted it three days ago and the sweep only runs now.
        await ctx.conn.execute(
            "UPDATE mirrors SET sent_at = ?",
            ((datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
        await ctx.conn.commit()
        adapter.deleted.clear()

        await PurgeWorker(ctx.repo, ctx.settings, ctx.adapters).sweep()

        mirror = (await ctx.repo.list_mirrors(row["id"]))[0]
        assert mirror["purged_at"] is None
        assert "48" in mirror["purge_error"]
        assert all(chat != "200" for chat, _ in adapter.deleted)


class TestPreview:
    def test_a_huge_photo_is_cut_down_to_chat_size(self, tmp_path):
        source = tmp_path / "big.jpg"
        Image.new("RGB", (6000, 4000), (200, 40, 40)).save(source, format="JPEG")

        assert make_preview(source, tmp_path / "small.jpg") is not None

        with Image.open(tmp_path / "small.jpg") as preview:
            assert max(preview.size) == 2560
        assert (tmp_path / "small.jpg").stat().st_size < source.stat().st_size

    def test_the_original_is_never_touched(self, tmp_path, jpeg_bytes):
        source = tmp_path / "original.jpg"
        source.write_bytes(jpeg_bytes)

        make_preview(source, tmp_path / "preview.jpg")

        assert source.read_bytes() == jpeg_bytes

    def test_a_sideways_photo_is_turned_upright(self, tmp_path):
        source = tmp_path / "rotated.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90°
        Image.new("RGB", (400, 200), (10, 10, 10)).save(source, format="JPEG", exif=exif)

        make_preview(source, tmp_path / "upright.jpg")

        with Image.open(tmp_path / "upright.jpg") as preview:
            assert preview.size == (200, 400)

    def test_something_that_is_not_an_image_yields_nothing(self, tmp_path):
        source = tmp_path / "notes.txt"
        source.write_bytes(b"just some text")

        assert make_preview(source, tmp_path / "preview.jpg") is None
        assert not (tmp_path / "preview.jpg").exists()

    def test_a_preview_carries_no_exif_trail(self, tmp_path, jpeg_with_exif):
        source = tmp_path / "gps.jpg"
        source.write_bytes(jpeg_with_exif("2018:01:02 03:04:05"))

        make_preview(source, tmp_path / "preview.jpg")

        with Image.open(tmp_path / "preview.jpg") as preview:
            assert not preview.getexif().get_ifd(0x8769)


class TestAlbums:
    async def test_every_photo_of_an_album_reaches_the_family(
        self, ctx, adapter, tmp_path, jpeg_bytes
    ):
        await make_storage(ctx, tmp_path)
        sender, *_ = await family(ctx, chats=("100", "200"))
        for n in range(3):
            adapter.put(f"ref-{n}", jpeg_bytes + bytes([n]))
            await ctx.repo.create_upload(
                media(
                    f"ref-{n}", name=f"IMG_{n}.JPG", message_id=str(10 + n),
                    kind=SourceKind.DOCUMENT, media_group_id="album-1",
                ),
                sender.id,
            )

        await drain(worker(ctx, adapter))

        assert len(photos_in(adapter, "200")) == 3


class TestTelegramSendsIt:
    """The one place that talks to Telegram: sendPhoto, and what comes back."""

    class StubBot:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def send_photo(self, chat_id, photo, caption=None):
            self.calls.append((chat_id, photo, caption))
            return SimpleNamespace(
                message_id=17,
                photo=[
                    SimpleNamespace(file_id="thumb"),
                    SimpleNamespace(file_id="full-size"),
                ],
            )

    async def test_bytes_go_up_and_a_reusable_handle_comes_back(self, tmp_path, jpeg_bytes):
        from aiogram.types import FSInputFile

        source = tmp_path / "preview.jpg"
        source.write_bytes(jpeg_bytes)
        bot = self.StubBot()

        sent = await TelegramAdapter(bot).send_photo("100", source, "📸 Мама")

        assert isinstance(bot.calls[0][1], FSInputFile)
        assert sent.message_id == "17"
        # The largest size is the one worth re-sending.
        assert sent.reusable_ref == "full-size"

    async def test_a_handle_is_passed_straight_through(self, tmp_path):
        bot = self.StubBot()

        await TelegramAdapter(bot).send_photo("200", "full-size", None)

        assert bot.calls[0][1] == "full-size"

    async def test_a_long_caption_is_trimmed_before_telegram_trims_it(self, tmp_path, jpeg_bytes):
        source = tmp_path / "preview.jpg"
        source.write_bytes(jpeg_bytes)
        bot = self.StubBot()

        await TelegramAdapter(bot).send_photo("100", source, "я" * 2000)

        assert len(bot.calls[0][2]) == CAPTION_LIMIT
