"""Family membership: who may use the bot, and where their photos land."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core import naming
from ..core.crypto import new_invite_code
from ..core.models import FolderMode, Group, Person, Platform, Role
from ..db.repo import Repo
from .settings import SettingsService

log = logging.getLogger(__name__)


@dataclass(slots=True)
class JoinResult:
    ok: bool
    person: Person | None = None
    reason: str | None = None
    made_admin: bool = False


class MemberService:
    def __init__(self, repo: Repo, settings_service: SettingsService) -> None:
        self.repo = repo
        self.settings = settings_service

    # ------------------------------------------------------------------ lookup

    async def resolve(
        self, platform: Platform, user_id: str
    ) -> tuple[Person, Group | None] | None:
        found = await self.repo.get_person_by_account(platform, user_id)
        if not found:
            return None
        person, _ = found
        return person, await self.repo.get_group(person.group_id)

    async def touch_account(
        self,
        person: Person,
        platform: Platform,
        user_id: str,
        username: str | None,
        chat_id: str | None,
    ) -> None:
        """Keep the chat id fresh so we can reach people with admin alerts."""
        await self.repo.link_account(person.id, platform, user_id, username, chat_id)

    # ---------------------------------------------------------------- onboarding

    async def bootstrap_admin(
        self,
        token_supplied: str,
        expected_token: str | None,
        platform: Platform,
        user_id: str,
        name: str,
        chat_id: str | None,
    ) -> JoinResult:
        """Claim the very first admin seat with the token from ``.env``.

        Only works while there are no people at all, so a leaked token cannot be
        replayed later to hijack an established install.
        """
        if not expected_token or token_supplied != expected_token:
            return JoinResult(ok=False, reason="bad_token")
        if await self.repo.count_people() > 0:
            return JoinResult(ok=False, reason="already_bootstrapped")

        person = await self.repo.create_person(name=name, role=Role.ADMIN)
        await self.repo.link_account(person.id, platform, user_id, name, chat_id)
        log.info("bootstrapped admin %s (%s)", name, platform)
        return JoinResult(ok=True, person=person, made_admin=True)

    async def join_with_code(
        self,
        code: str,
        platform: Platform,
        user_id: str,
        name: str,
        chat_id: str | None,
    ) -> JoinResult:
        existing = await self.repo.get_person_by_account(platform, user_id)
        if existing:
            return JoinResult(ok=False, person=existing[0], reason="already_member")

        invite = await self.repo.get_invite(code)
        if not invite:
            return JoinResult(ok=False, reason="bad_code")
        # consume_invite re-checks expiry/uses atomically; this is just a fast path.
        if not await self.repo.consume_invite(code):
            return JoinResult(ok=False, reason="bad_code")

        person = await self.repo.create_person(
            name=name, role=Role(invite["role"]), group_id=invite["group_id"]
        )
        await self.repo.link_account(person.id, platform, user_id, name, chat_id)
        log.info("%s joined as %s via invite", name, invite["role"])
        return JoinResult(ok=True, person=person)

    async def create_invite(
        self,
        created_by: int | None,
        role: Role = Role.MEMBER,
        group_id: int | None = None,
        ttl_hours: int | None = 72,
        max_uses: int = 1,
    ) -> dict:
        code = new_invite_code()
        expires_at = datetime.now() + timedelta(hours=ttl_hours) if ttl_hours else None
        return await self.repo.create_invite(
            code=code,
            created_by=created_by,
            role=role,
            group_id=group_id,
            expires_at=expires_at,
            max_uses=max_uses,
        )

    # ------------------------------------------------------------------ folders

    async def folder_preview(self, person: Person) -> str:
        """The folder this person's next photo would go to — shown in /me."""
        settings = await self.settings.get()
        group = await self.repo.get_group(person.group_id)
        storage = await self.repo.get_default_storage()
        root = (storage or {}).get("root_folder") or settings.root_folder

        owner = naming.owner_segment(person, group, settings.folder_mode)
        now = datetime.now()
        ctx = naming.build_context(
            root=root,
            owner=owner,
            taken_at=now,
            filename="IMG_0001.JPG",
            person=person,
            group=group,
        )
        return naming.build_remote_dir(settings.dir_template, ctx)

    async def effective_mode(self, person: Person) -> FolderMode:
        settings = await self.settings.get()
        return person.folder_mode_override or settings.folder_mode
