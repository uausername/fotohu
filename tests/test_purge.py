"""Chat cleanup, including the Telegram 48-hour ceiling and Viber's missing API."""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import FakeAdapter

from fotohu.core.models import Platform
from fotohu.services.settings import Settings
from fotohu.worker.purger import PurgeWorker


def ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def seed_done_upload(
    ctx,
    *,
    message_id="5",
    received_at: datetime | None = None,
    purge_after: datetime | None = None,
    platform=Platform.TELEGRAM,
    bot_message_id: str | None = None,
):
    """Insert a row that already reached the cloud and is due for purging."""
    now = datetime.now()
    await ctx.conn.execute(
        "INSERT INTO uploads (platform, chat_id, message_id, source_kind, file_name,"
        " state, remote_path, received_at, uploaded_at, purge_after, bot_message_id)"
        " VALUES (?, '100', ?, 'document', 'a.jpg', 'done', 'FotoHu/a.jpg', ?, ?, ?, ?)",
        (
            str(platform),
            message_id,
            ts(received_at or now),
            ts(now),
            ts(purge_after or (now - timedelta(minutes=1))),
            bot_message_id,
        ),
    )
    await ctx.conn.commit()


def purger(ctx, adapter, platform=Platform.TELEGRAM):
    ctx.adapters[platform] = adapter
    return PurgeWorker(ctx.repo, ctx.settings, ctx.adapters)


class TestPurge:
    async def test_a_due_message_is_deleted(self, ctx, adapter):
        await seed_done_upload(ctx)
        assert await purger(ctx, adapter).sweep() == 1
        assert adapter.deleted == [("100", ["5"])]
        assert (await ctx.repo.recent_uploads())[0]["purged_at"] is not None

    async def test_a_message_not_yet_due_is_left_alone(self, ctx, adapter):
        await seed_done_upload(ctx, purge_after=datetime.now() + timedelta(hours=2))
        assert await purger(ctx, adapter).sweep() == 0
        assert adapter.deleted == []

    async def test_an_upload_still_in_flight_is_never_purged(self, ctx, adapter):
        # The safety property: never remove the chat copy before the cloud copy exists.
        await ctx.conn.execute(
            "INSERT INTO uploads (platform, chat_id, message_id, source_kind, file_name,"
            " state, purge_after) VALUES ('telegram','100','7','document','a.jpg',"
            " 'pending', ?)",
            (ts(datetime.now() - timedelta(hours=1)),),
        )
        await ctx.conn.commit()
        assert await purger(ctx, adapter).sweep() == 0
        assert adapter.deleted == []

    async def test_the_bots_own_reply_is_removed_too(self, ctx, adapter):
        await seed_done_upload(ctx, bot_message_id="6")
        await purger(ctx, adapter).sweep()
        assert adapter.deleted == [("100", ["5", "6"])]

    async def test_bot_replies_can_be_kept(self, ctx, adapter):
        await ctx.settings.set("purge_bot_replies", False)
        await seed_done_upload(ctx, bot_message_id="6")
        await purger(ctx, adapter).sweep()
        assert adapter.deleted == [("100", ["5"])]

    async def test_purging_can_be_switched_off(self, ctx, adapter):
        await ctx.settings.set("purge_enabled", False)
        await seed_done_upload(ctx)
        assert await purger(ctx, adapter).sweep() == 0
        assert adapter.deleted == []


class TestTelegramDeleteWindow:
    async def test_messages_past_48_hours_are_not_even_attempted(self, ctx, adapter):
        await seed_done_upload(ctx, received_at=datetime.now() - timedelta(hours=50))

        assert await purger(ctx, adapter).sweep() == 0
        # No pointless API call, and the reason is recorded for the admin panel.
        assert adapter.deleted == []
        row = (await ctx.repo.recent_uploads())[0]
        assert row["purged_at"] is None
        assert "48" in row["purge_error"]

    async def test_a_message_just_inside_the_window_is_still_deleted(self, ctx, adapter):
        await seed_done_upload(ctx, received_at=datetime.now() - timedelta(hours=47))
        assert await purger(ctx, adapter).sweep() == 1

    async def test_a_refusal_from_telegram_is_recorded_not_retried_forever(self, ctx, adapter):
        adapter.undeletable["5"] = "older than 48h — Telegram refuses to delete it"
        await seed_done_upload(ctx)

        assert await purger(ctx, adapter).sweep() == 0
        row = (await ctx.repo.recent_uploads())[0]
        assert row["purge_error"]

        # A row with purge_error is not picked up again on the next sweep.
        adapter.deleted.clear()
        await purger(ctx, adapter).sweep()
        assert adapter.deleted == []

    async def test_a_failing_reply_does_not_mark_the_photo_purged(self, ctx, adapter):
        adapter.undeletable["6"] = "too old"
        await seed_done_upload(ctx, bot_message_id="6")
        assert await purger(ctx, adapter).sweep() == 0
        assert (await ctx.repo.recent_uploads())[0]["purged_at"] is None


class TestViber:
    async def test_viber_is_marked_unsupported_rather_than_retried(self, ctx):
        viber = FakeAdapter(platform=Platform.VIBER, supports_deletion=False,
                            delete_window_hours=None)
        await seed_done_upload(ctx, platform=Platform.VIBER)

        assert await purger(ctx, viber, Platform.VIBER).sweep() == 0
        assert viber.deleted == []
        row = (await ctx.repo.recent_uploads())[0]
        assert "no message-deletion API" in row["purge_error"]


class TestSettingsGuardrails:
    def test_the_48_hour_ceiling_is_flagged(self):
        assert Settings(purge_after_hours=1).purge_exceeds_telegram_window is False
        assert Settings(purge_after_hours=47).purge_exceeds_telegram_window is False
        assert Settings(purge_after_hours=48).purge_exceeds_telegram_window is True
        assert Settings(purge_after_hours=72).purge_exceeds_telegram_window is True

    async def test_setting_a_value_over_48h_warns_but_is_accepted(self, ctx):
        from fotohu.services.admin import AdminService

        admin = AdminService(ctx.repo, ctx.settings, ctx.members, ctx.storage)
        result = await admin.set_purge_hours("72")
        assert result.ok
        assert "48" in result.message
        assert (await ctx.settings.get()).purge_after_hours == 72

    async def test_a_non_numeric_value_is_rejected(self, ctx):
        from fotohu.services.admin import AdminService

        admin = AdminService(ctx.repo, ctx.settings, ctx.members, ctx.storage)
        result = await admin.set_purge_hours("завтра")
        assert not result.ok
        assert (await ctx.settings.get()).purge_after_hours == 1
