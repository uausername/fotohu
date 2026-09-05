"""What an album says back: a receipt on arrival, one summary once it lands."""

from __future__ import annotations

from fotohu.core.models import Platform
from fotohu.i18n import t
from fotohu.messengers.telegram import handlers
from fotohu.worker.uploader import UploadWorker

GROUP = "album-1"


async def seed_album(ctx, group_id: str = GROUP, files: int = 4, state: str = "pending"):
    """Insert one row per album member, the way the media handler would."""
    ids = []
    for n in range(files):
        cur = await ctx.conn.execute(
            "INSERT INTO uploads (platform, chat_id, message_id, media_group_id,"
            " source_kind, file_name, state, received_at)"
            " VALUES ('telegram', '100', ?, ?, 'document', ?, ?, '2026-03-14 09:26:53')",
            (str(10 + n), group_id, f"IMG_{n}.JPG", state),
        )
        ids.append(cur.lastrowid)
    await ctx.conn.commit()
    return ids


def uploader(ctx, adapter) -> UploadWorker:
    ctx.adapters[Platform.TELEGRAM] = adapter
    return UploadWorker(ctx.repo, ctx.settings, ctx.storage, ctx.adapters, ctx.config.temp_dir)


class TestArrivalReceipt:
    async def test_an_album_is_answered_before_it_finishes_uploading(
        self, ctx, adapter, monkeypatch
    ):
        monkeypatch.setattr(handlers, "ALBUM_ACK_DELAY", 0)
        await seed_album(ctx)

        await handlers._ack_album(adapter, ctx, "ru", GROUP, "100")

        assert adapter.sent == [("100", t("ru", "album.queued", n=4))]

    async def test_only_the_first_file_of_an_album_claims_the_receipt(self):
        handlers._answered_albums.clear()

        assert handlers._claim_album_receipt("album-9") is True
        assert handlers._claim_album_receipt("album-9") is False

    async def test_the_album_memory_stays_bounded(self):
        handlers._answered_albums.clear()

        for n in range(handlers.ALBUM_MEMORY + 50):
            handlers._claim_album_receipt(f"album-{n}")

        assert len(handlers._answered_albums) == handlers.ALBUM_MEMORY


class TestFinalSummary:
    async def test_a_finished_album_is_summarised_once(self, ctx, adapter):
        ids = await seed_album(ctx, files=2, state="done")
        settings = await ctx.settings.get()
        worker = uploader(ctx, adapter)
        record = await ctx.repo.get_upload(ids[-1])

        # Two slots finishing in the same instant both see a complete album.
        await worker._report(adapter, record, settings, "per-file text, unused here")
        await worker._report(adapter, record, settings, "per-file text, unused here")

        assert len(adapter.sent) == 1
        assert t(settings.language, "album.done", n=2) in adapter.sent[0][1]

    async def test_a_sibling_that_succeeds_on_retry_reports_the_new_tally(self, ctx, adapter):
        ids = await seed_album(ctx, files=2, state="done")
        await ctx.repo.update_upload(ids[0], state="failed")
        settings = await ctx.settings.get()
        worker = uploader(ctx, adapter)

        await worker._report(
            adapter, await ctx.repo.get_upload(ids[1]), settings, "unused"
        )
        await ctx.repo.update_upload(ids[0], state="done")
        await worker._report(
            adapter, await ctx.repo.get_upload(ids[0]), settings, "unused"
        )

        assert len(adapter.sent) == 2
        assert t(settings.language, "album.failed", n=1) in adapter.sent[0][1]
        assert t(settings.language, "album.done", n=2) in adapter.sent[1][1]
