"""Data access. Every SQL statement in the project lives in this module."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from ..core.models import (
    Account,
    FolderMode,
    Group,
    IncomingMedia,
    Person,
    Platform,
    Role,
    UploadState,
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _person(row: aiosqlite.Row) -> Person:
    return Person(
        id=row["id"],
        name=row["name"],
        role=Role(row["role"]),
        status=row["status"],
        personal_folder=row["personal_folder"],
        group_id=row["group_id"],
        folder_mode_override=(
            FolderMode(row["folder_mode_override"]) if row["folder_mode_override"] else None
        ),
    )


def _group(row: aiosqlite.Row) -> Group:
    return Group(id=row["id"], name=row["name"], folder=row["folder"])


def _account(row: aiosqlite.Row) -> Account:
    return Account(
        id=row["id"],
        person_id=row["person_id"],
        platform=Platform(row["platform"]),
        platform_user_id=row["platform_user_id"],
        username=row["username"],
        chat_id=row["chat_id"],
    )


class Repo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ settings

    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self.conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value_json"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
            " updated_at = excluded.updated_at",
            (key, json.dumps(value), _now()),
        )
        await self.conn.commit()

    async def all_settings(self) -> dict[str, Any]:
        cur = await self.conn.execute("SELECT key, value_json FROM settings")
        return {r["key"]: json.loads(r["value_json"]) for r in await cur.fetchall()}

    # -------------------------------------------------------------------- people

    async def get_person(self, person_id: int) -> Person | None:
        cur = await self.conn.execute("SELECT * FROM people WHERE id = ?", (person_id,))
        row = await cur.fetchone()
        return _person(row) if row else None

    async def get_account(self, platform: Platform, platform_user_id: str) -> Account | None:
        cur = await self.conn.execute(
            "SELECT * FROM accounts WHERE platform = ? AND platform_user_id = ?",
            (str(platform), str(platform_user_id)),
        )
        row = await cur.fetchone()
        return _account(row) if row else None

    async def get_person_by_account(
        self, platform: Platform, platform_user_id: str
    ) -> tuple[Person, Account] | None:
        cur = await self.conn.execute(
            "SELECT p.*, a.id AS acc_id, a.person_id AS acc_person_id, a.platform AS acc_platform,"
            "       a.platform_user_id AS acc_uid, a.username AS acc_username,"
            "       a.chat_id AS acc_chat_id"
            "  FROM accounts a JOIN people p ON p.id = a.person_id"
            " WHERE a.platform = ? AND a.platform_user_id = ?",
            (str(platform), str(platform_user_id)),
        )
        row = await cur.fetchone()
        if not row:
            return None
        account = Account(
            id=row["acc_id"],
            person_id=row["acc_person_id"],
            platform=Platform(row["acc_platform"]),
            platform_user_id=row["acc_uid"],
            username=row["acc_username"],
            chat_id=row["acc_chat_id"],
        )
        return _person(row), account

    async def list_people(self) -> list[Person]:
        cur = await self.conn.execute("SELECT * FROM people ORDER BY role, name")
        return [_person(r) for r in await cur.fetchall()]

    async def count_people(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM people")
        return (await cur.fetchone())["n"]

    async def create_person(
        self,
        name: str,
        role: Role = Role.MEMBER,
        group_id: int | None = None,
        personal_folder: str | None = None,
    ) -> Person:
        cur = await self.conn.execute(
            "INSERT INTO people (name, role, group_id, personal_folder) VALUES (?, ?, ?, ?)",
            (name, str(role), group_id, personal_folder),
        )
        await self.conn.commit()
        person = await self.get_person(cur.lastrowid)
        assert person is not None
        return person

    async def update_person(self, person_id: int, **fields: Any) -> None:
        allowed = {
            "name", "role", "status", "personal_folder", "group_id", "folder_mode_override",
        }
        updates = {
            k: (str(v) if v is not None else None) for k, v in fields.items() if k in allowed
        }
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE people SET {assignments} WHERE id = ?",  # noqa: S608 - keys are allow-listed
            (*updates.values(), person_id),
        )
        await self.conn.commit()

    async def delete_person(self, person_id: int) -> None:
        await self.conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
        await self.conn.commit()

    async def link_account(
        self,
        person_id: int,
        platform: Platform,
        platform_user_id: str,
        username: str | None = None,
        chat_id: str | None = None,
    ) -> Account:
        await self.conn.execute(
            "INSERT INTO accounts (person_id, platform, platform_user_id, username, chat_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(platform, platform_user_id) DO UPDATE SET"
            "   person_id = excluded.person_id, username = excluded.username,"
            "   chat_id = COALESCE(excluded.chat_id, accounts.chat_id)",
            (person_id, str(platform), str(platform_user_id), username, chat_id),
        )
        await self.conn.commit()
        account = await self.get_account(platform, platform_user_id)
        assert account is not None
        return account

    async def list_accounts(self, person_id: int) -> list[Account]:
        cur = await self.conn.execute(
            "SELECT * FROM accounts WHERE person_id = ? ORDER BY platform", (person_id,)
        )
        return [_account(r) for r in await cur.fetchall()]

    async def list_admin_accounts(self) -> list[Account]:
        cur = await self.conn.execute(
            "SELECT a.* FROM accounts a JOIN people p ON p.id = a.person_id"
            " WHERE p.role = 'admin' AND p.status = 'active'"
        )
        return [_account(r) for r in await cur.fetchall()]

    # -------------------------------------------------------------------- groups

    async def create_group(self, name: str, folder: str | None = None) -> Group:
        cur = await self.conn.execute(
            "INSERT INTO groups (name, folder) VALUES (?, ?)", (name, folder or name)
        )
        await self.conn.commit()
        return Group(id=cur.lastrowid, name=name, folder=folder or name)

    async def get_group(self, group_id: int | None) -> Group | None:
        if group_id is None:
            return None
        cur = await self.conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
        row = await cur.fetchone()
        return _group(row) if row else None

    async def get_group_by_name(self, name: str) -> Group | None:
        cur = await self.conn.execute("SELECT * FROM groups WHERE name = ?", (name,))
        row = await cur.fetchone()
        return _group(row) if row else None

    async def list_groups(self) -> list[Group]:
        cur = await self.conn.execute("SELECT * FROM groups ORDER BY name")
        return [_group(r) for r in await cur.fetchall()]

    async def delete_group(self, group_id: int) -> None:
        await self.conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        await self.conn.commit()

    # ------------------------------------------------------------------- invites

    async def create_invite(
        self,
        code: str,
        created_by: int | None,
        role: Role = Role.MEMBER,
        group_id: int | None = None,
        expires_at: datetime | None = None,
        max_uses: int = 1,
    ) -> dict[str, Any]:
        await self.conn.execute(
            "INSERT INTO invites (code, created_by, role, group_id, expires_at, max_uses)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code, created_by, str(role), group_id, _ts(expires_at), max_uses),
        )
        await self.conn.commit()
        return {
            "code": code, "role": str(role), "group_id": group_id,
            "expires_at": _ts(expires_at), "max_uses": max_uses, "uses": 0,
        }

    async def get_invite(self, code: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM invites WHERE code = ?", (code.upper(),))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_invites(self, include_spent: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM invites"
        if not include_spent:
            sql += " WHERE revoked_at IS NULL AND uses < max_uses"
        cur = await self.conn.execute(sql + " ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def consume_invite(self, code: str) -> bool:
        """Atomically spend one use. False when spent, revoked or expired."""
        cur = await self.conn.execute(
            "UPDATE invites SET uses = uses + 1"
            " WHERE code = ? AND revoked_at IS NULL AND uses < max_uses"
            "   AND (expires_at IS NULL OR expires_at > ?)",
            (code.upper(), _now()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def revoke_invite(self, code: str) -> None:
        await self.conn.execute(
            "UPDATE invites SET revoked_at = ? WHERE code = ?", (_now(), code.upper())
        )
        await self.conn.commit()

    # ----------------------------------------------------------- storage accounts

    async def create_storage_account(
        self, backend: str, label: str, root_folder: str = "FotoHu", extra: dict | None = None
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO storage_accounts (backend, label, root_folder, extra_json)"
            " VALUES (?, ?, ?, ?)",
            (backend, label, root_folder, json.dumps(extra or {})),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_storage_account(self, account_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM storage_accounts WHERE id = ?", (account_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_default_storage(self) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM storage_accounts WHERE is_default = 1 LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_storage_accounts(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM storage_accounts ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def set_default_storage(self, account_id: int) -> None:
        await self.conn.execute("UPDATE storage_accounts SET is_default = 0")
        await self.conn.execute(
            "UPDATE storage_accounts SET is_default = 1 WHERE id = ?", (account_id,)
        )
        await self.conn.commit()

    async def update_storage_account(self, account_id: int, **fields: Any) -> None:
        allowed = {"credentials_enc", "root_folder", "label", "extra_json"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE storage_accounts SET {assignments} WHERE id = ?",  # noqa: S608
            (*updates.values(), account_id),
        )
        await self.conn.commit()

    async def delete_storage_account(self, account_id: int) -> None:
        await self.conn.execute("DELETE FROM storage_accounts WHERE id = ?", (account_id,))
        await self.conn.commit()

    # -------------------------------------------------------------- folder cache

    async def get_cached_folder(self, storage_account_id: int, path: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT remote_id FROM folder_cache WHERE storage_account_id = ? AND path = ?",
            (storage_account_id, path),
        )
        row = await cur.fetchone()
        return row["remote_id"] if row else None

    async def put_cached_folder(
        self, storage_account_id: int, path: str, remote_id: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO folder_cache (storage_account_id, path, remote_id) VALUES (?, ?, ?)"
            " ON CONFLICT(storage_account_id, path) DO UPDATE SET remote_id = excluded.remote_id",
            (storage_account_id, path, remote_id),
        )
        await self.conn.commit()

    async def clear_folder_cache(self, storage_account_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM folder_cache WHERE storage_account_id = ?", (storage_account_id,)
        )
        await self.conn.commit()

    # ------------------------------------------------------------------- uploads

    async def create_upload(
        self, media: IncomingMedia, person_id: int | None
    ) -> int | None:
        """Insert a pending row. Returns ``None`` if this message is already known,
        which is how we stay idempotent against redelivered webhook updates."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO uploads"
            " (person_id, platform, chat_id, message_id, media_group_id, source_kind,"
            "  lossless, remote_file_id, file_name, size, caption, taken_at, state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                person_id,
                str(media.platform),
                str(media.chat_id),
                str(media.message_id),
                media.media_group_id,
                str(media.source_kind),
                int(media.lossless),
                media.file_ref,
                media.file_name,
                media.size,
                media.caption,
                _ts(media.sent_at),
            ),
        )
        await self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    async def get_upload(self, upload_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def claim_next_upload(self) -> dict[str, Any] | None:
        """Atomically move one due row to 'uploading' so parallel workers don't collide."""
        cur = await self.conn.execute(
            "UPDATE uploads SET state = 'uploading', attempts = attempts + 1"
            " WHERE id = (SELECT id FROM uploads"
            "             WHERE state IN ('pending', 'failed')"
            "               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            "               AND attempts < 6"
            "             ORDER BY received_at LIMIT 1)"
            " RETURNING *",
            (_now(),),
        )
        row = await cur.fetchone()
        await self.conn.commit()
        return dict(row) if row else None

    async def find_duplicate(self, sha256: str, remote_dir: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM uploads WHERE sha256 = ? AND remote_path LIKE ? AND state = 'done'"
            " LIMIT 1",
            (sha256, f"{remote_dir}/%"),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_upload(self, upload_id: int, **fields: Any) -> None:
        allowed = {
            "person_id", "file_name", "size", "sha256", "md5", "taken_at", "date_source",
            "storage_account_id", "backend", "remote_path", "remote_id", "verified", "state",
            "last_error", "uploaded_at", "next_attempt_at", "purge_after", "purged_at",
            "purge_error", "bot_message_id", "lossless",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE uploads SET {assignments} WHERE id = ?",  # noqa: S608 - allow-listed keys
            (*updates.values(), upload_id),
        )
        await self.conn.commit()

    async def mark_failed(self, upload_id: int, error: str, retry_in: timedelta | None) -> None:
        await self.update_upload(
            upload_id,
            state=str(UploadState.FAILED),
            last_error=error[:500],
            next_attempt_at=_ts(datetime.now() + retry_in) if retry_in else None,
        )

    async def mark_done(
        self,
        upload_id: int,
        *,
        remote_path: str,
        remote_id: str,
        backend: str,
        storage_account_id: int,
        verified: bool,
        purge_after: datetime | None,
    ) -> None:
        await self.update_upload(
            upload_id,
            state=str(UploadState.DONE),
            remote_path=remote_path,
            remote_id=remote_id,
            backend=backend,
            storage_account_id=storage_account_id,
            verified=int(verified),
            uploaded_at=_now(),
            purge_after=_ts(purge_after),
            last_error=None,
        )

    async def due_for_purge(self, limit: int = 200) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM uploads"
            " WHERE state = 'done' AND purged_at IS NULL AND purge_error IS NULL"
            "   AND purge_after IS NOT NULL AND purge_after <= ?"
            " ORDER BY purge_after LIMIT ?",
            (_now(), limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def mark_purged(self, upload_ids: list[int]) -> None:
        if not upload_ids:
            return
        placeholders = ",".join("?" * len(upload_ids))
        await self.conn.execute(
            f"UPDATE uploads SET purged_at = ? WHERE id IN ({placeholders})",  # noqa: S608
            (_now(), *upload_ids),
        )
        await self.conn.commit()

    async def mark_purge_failed(self, upload_ids: list[int], error: str) -> None:
        if not upload_ids:
            return
        placeholders = ",".join("?" * len(upload_ids))
        await self.conn.execute(
            f"UPDATE uploads SET purge_error = ? WHERE id IN ({placeholders})",  # noqa: S608
            (error[:300], *upload_ids),
        )
        await self.conn.commit()

    async def media_group_progress(self, media_group_id: str) -> dict[str, Any]:
        """How an album is doing, so we can post one summary instead of N replies."""
        cur = await self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM uploads WHERE media_group_id = ? GROUP BY state",
            (media_group_id,),
        )
        counts = {r["state"]: r["n"] for r in await cur.fetchall()}
        in_flight = counts.get("pending", 0) + counts.get("uploading", 0)
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "in_flight": in_flight,
            "finished": in_flight == 0,
            "done": counts.get("done", 0),
            "duplicates": counts.get("skipped_dup", 0),
            "rejected": counts.get("rejected", 0),
            "failed": counts.get("failed", 0),
        }

    async def recent_uploads(self, person_id: int | None = None, limit: int = 10) -> list[dict]:
        if person_id is None:
            cur = await self.conn.execute(
                "SELECT * FROM uploads ORDER BY received_at DESC LIMIT ?", (limit,)
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM uploads WHERE person_id = ? ORDER BY received_at DESC LIMIT ?",
                (person_id, limit),
            )
        return [dict(r) for r in await cur.fetchall()]

    async def reset_failed(self) -> int:
        cur = await self.conn.execute(
            "UPDATE uploads SET state = 'pending', attempts = 0, next_attempt_at = NULL,"
            " last_error = NULL WHERE state = 'failed'"
        )
        await self.conn.commit()
        return cur.rowcount

    async def stats(self) -> dict[str, Any]:
        cur = await self.conn.execute(
            "SELECT state, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes"
            " FROM uploads GROUP BY state"
        )
        by_state = {
            r["state"]: {"count": r["n"], "bytes": r["bytes"]} for r in await cur.fetchall()
        }

        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM uploads"
            " WHERE state = 'done' AND uploaded_at >= date('now', 'start of month')"
        )
        month = dict(await cur.fetchone())

        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE purge_error IS NOT NULL"
        )
        purge_failed = (await cur.fetchone())["n"]

        cur = await self.conn.execute(
            "SELECT last_error, file_name, received_at FROM uploads"
            " WHERE state = 'failed' AND last_error IS NOT NULL"
            " ORDER BY received_at DESC LIMIT 5"
        )
        errors = [dict(r) for r in await cur.fetchall()]

        return {
            "by_state": by_state,
            "month": month,
            "purge_failed": purge_failed,
            "recent_errors": errors,
        }

    async def per_person_stats(self) -> dict[int, dict[str, int]]:
        cur = await self.conn.execute(
            "SELECT person_id, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes"
            " FROM uploads WHERE state = 'done' GROUP BY person_id"
        )
        return {
            r["person_id"]: {"count": r["n"], "bytes": r["bytes"]}
            for r in await cur.fetchall()
            if r["person_id"] is not None
        }

    # -------------------------------------------------------------- oauth states

    async def create_oauth_state(
        self,
        state: str,
        backend: str,
        person_id: int | None,
        verifier: str | None,
        payload: dict[str, Any],
        ttl_minutes: int = 10,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO oauth_states (state, backend, person_id, verifier, payload, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                state, backend, person_id, verifier, json.dumps(payload),
                _ts(datetime.now() + timedelta(minutes=ttl_minutes)),
            ),
        )
        await self.conn.commit()

    async def consume_oauth_state(self, state: str) -> dict[str, Any] | None:
        """Single-use: the same state can never be redeemed twice."""
        cur = await self.conn.execute(
            "UPDATE oauth_states SET used_at = ?"
            " WHERE state = ? AND used_at IS NULL AND expires_at > ?"
            " RETURNING *",
            (_now(), state, _now()),
        )
        row = await cur.fetchone()
        await self.conn.commit()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return data

    async def purge_expired_oauth_states(self) -> None:
        await self.conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (_now(),))
        await self.conn.commit()
