"""Family access: bootstrap, invites, roles, per-person folder overrides."""

from __future__ import annotations

from datetime import datetime, timedelta

from fotohu.core.models import FolderMode, Platform, Role
from fotohu.services.admin import AdminService


def admin_service(ctx) -> AdminService:
    return AdminService(ctx.repo, ctx.settings, ctx.members, ctx.storage)


class TestBootstrap:
    async def test_the_first_user_with_the_token_becomes_admin(self, ctx):
        result = await ctx.members.bootstrap_admin(
            "BOOTSTRAP", "BOOTSTRAP", Platform.TELEGRAM, "1", "Дмитрий", "100"
        )
        assert result.ok and result.made_admin
        assert result.person.role == Role.ADMIN

    async def test_a_wrong_token_is_refused(self, ctx):
        result = await ctx.members.bootstrap_admin(
            "nope", "BOOTSTRAP", Platform.TELEGRAM, "1", "X", "100"
        )
        assert not result.ok and result.reason == "bad_token"
        assert await ctx.repo.count_people() == 0

    async def test_the_token_cannot_be_replayed_once_someone_exists(self, ctx):
        await ctx.members.bootstrap_admin(
            "BOOTSTRAP", "BOOTSTRAP", Platform.TELEGRAM, "1", "Дмитрий", "100"
        )
        second = await ctx.members.bootstrap_admin(
            "BOOTSTRAP", "BOOTSTRAP", Platform.TELEGRAM, "2", "Незнакомец", "200"
        )
        assert not second.ok and second.reason == "already_bootstrapped"
        assert await ctx.repo.count_people() == 1


class TestInvites:
    async def test_a_valid_code_admits_one_person(self, ctx):
        invite = await ctx.members.create_invite(created_by=None)
        result = await ctx.members.join_with_code(
            invite["code"], Platform.TELEGRAM, "2", "Мама", "200"
        )
        assert result.ok
        assert result.person.role == Role.MEMBER

    async def test_a_single_use_code_cannot_be_used_twice(self, ctx):
        invite = await ctx.members.create_invite(created_by=None)
        assert (await ctx.members.join_with_code(
            invite["code"], Platform.TELEGRAM, "2", "Мама", "200")).ok
        second = await ctx.members.join_with_code(
            invite["code"], Platform.TELEGRAM, "3", "Чужой", "300"
        )
        assert not second.ok
        assert await ctx.repo.count_people() == 1

    async def test_a_multi_use_code_admits_exactly_that_many(self, ctx):
        invite = await ctx.members.create_invite(created_by=None, max_uses=2)
        for uid in ("2", "3"):
            assert (await ctx.members.join_with_code(
                invite["code"], Platform.TELEGRAM, uid, f"P{uid}", uid)).ok
        assert not (await ctx.members.join_with_code(
            invite["code"], Platform.TELEGRAM, "4", "P4", "4")).ok
        assert await ctx.repo.count_people() == 2

    async def test_an_expired_code_is_refused(self, ctx):
        await ctx.repo.create_invite(
            code="OLDCODE1", created_by=None,
            expires_at=datetime.now() - timedelta(hours=1),
        )
        result = await ctx.members.join_with_code(
            "OLDCODE1", Platform.TELEGRAM, "2", "Мама", "200"
        )
        assert not result.ok
        assert await ctx.repo.count_people() == 0

    async def test_a_revoked_code_is_refused(self, ctx):
        invite = await ctx.members.create_invite(created_by=None)
        await ctx.repo.revoke_invite(invite["code"])
        assert not (await ctx.members.join_with_code(
            invite["code"], Platform.TELEGRAM, "2", "Мама", "200")).ok

    async def test_an_unknown_code_is_refused(self, ctx):
        assert not (await ctx.members.join_with_code(
            "NOSUCHCD", Platform.TELEGRAM, "2", "Мама", "200")).ok

    async def test_a_code_can_carry_a_group(self, ctx):
        group = await ctx.repo.create_group("Родители", "parents")
        invite = await ctx.members.create_invite(created_by=None, group_id=group.id)
        result = await ctx.members.join_with_code(
            invite["code"], Platform.TELEGRAM, "2", "Мама", "200"
        )
        assert result.person.group_id == group.id

    async def test_joining_twice_is_reported_rather_than_duplicated(self, ctx):
        first = await ctx.members.create_invite(created_by=None)
        await ctx.members.join_with_code(first["code"], Platform.TELEGRAM, "2", "Мама", "200")
        second = await ctx.members.create_invite(created_by=None)
        result = await ctx.members.join_with_code(
            second["code"], Platform.TELEGRAM, "2", "Мама", "200"
        )
        assert not result.ok and result.reason == "already_member"
        assert await ctx.repo.count_people() == 1


class TestOneIdentityTwoMessengers:
    async def test_a_person_can_hold_both_a_telegram_and_a_viber_account(self, ctx):
        person = await ctx.repo.create_person("Дмитрий", Role.ADMIN)
        await ctx.repo.link_account(person.id, Platform.TELEGRAM, "1", "d", "100")
        await ctx.repo.link_account(person.id, Platform.VIBER, "abc==", "d", "abc==")

        from_tg = await ctx.members.resolve(Platform.TELEGRAM, "1")
        from_viber = await ctx.members.resolve(Platform.VIBER, "abc==")
        assert from_tg[0].id == from_viber[0].id == person.id

        # Both routes must land in the same folder.
        assert await ctx.members.folder_preview(from_tg[0]) == (
            await ctx.members.folder_preview(from_viber[0])
        )


class TestAdminOperations:
    async def test_the_last_admin_cannot_demote_themselves(self, ctx):
        person = await ctx.repo.create_person("Дмитрий", Role.ADMIN)
        result = await admin_service(ctx).toggle_role(person)
        assert not result.ok
        assert (await ctx.repo.get_person(person.id)).role == Role.ADMIN

    async def test_an_admin_can_be_demoted_when_another_one_remains(self, ctx):
        first = await ctx.repo.create_person("A", Role.ADMIN)
        await ctx.repo.create_person("B", Role.ADMIN)
        assert (await admin_service(ctx).toggle_role(first)).ok
        assert (await ctx.repo.get_person(first.id)).role == Role.MEMBER

    async def test_blocking_and_unblocking_round_trips(self, ctx):
        person = await ctx.repo.create_person("B", Role.MEMBER)
        await admin_service(ctx).toggle_block(person)
        blocked = await ctx.repo.get_person(person.id)
        assert not blocked.is_active
        await admin_service(ctx).toggle_block(blocked)
        assert (await ctx.repo.get_person(person.id)).is_active

    async def test_a_personal_override_changes_only_that_person(self, ctx):
        dad = await ctx.repo.create_person("Папа", Role.ADMIN)
        mum = await ctx.repo.create_person("Мама", Role.MEMBER)
        await admin_service(ctx).set_person_folder_mode(dad, FolderMode.SHARED)

        dad = await ctx.repo.get_person(dad.id)
        assert await ctx.members.effective_mode(dad) == FolderMode.SHARED
        assert await ctx.members.effective_mode(mum) == FolderMode.PER_PERSON

    async def test_assigning_a_group_warns_when_the_layout_ignores_groups(self, ctx):
        person = await ctx.repo.create_person("Папа", Role.ADMIN)
        group = await ctx.repo.create_group("Родители", "parents")
        result = await admin_service(ctx).set_person_group(person, group.id)
        assert result.ok
        assert "не «по группам»" in result.message

    async def test_no_warning_once_the_layout_does_use_groups(self, ctx):
        await ctx.settings.set("folder_mode", "per_group")
        person = await ctx.repo.create_person("Папа", Role.ADMIN)
        group = await ctx.repo.create_group("Родители", "parents")
        result = await admin_service(ctx).set_person_group(person, group.id)
        assert "⚠️" not in result.message

    async def test_duplicate_group_names_are_refused(self, ctx):
        service = admin_service(ctx)
        assert (await service.create_group("Родители")).ok
        assert not (await service.create_group("Родители")).ok
