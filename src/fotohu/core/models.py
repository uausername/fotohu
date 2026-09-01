"""Domain types shared by the messengers, the pipeline and the storage backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class Platform(StrEnum):
    TELEGRAM = "telegram"
    VIBER = "viber"


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class FolderMode(StrEnum):
    PER_PERSON = "per_person"
    SHARED = "shared"
    PER_GROUP = "per_group"


class PhotoPolicy(StrEnum):
    """What to do with media the messenger already re-compressed."""

    REJECT = "reject"           # refuse and explain how to send as a file
    SAVE_MARKED = "save_marked"  # keep it, but in a separate "_compressed" folder
    SAVE = "save"                # keep it as if it were an original


class UploadState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    SKIPPED_DUP = "skipped_dup"
    REJECTED = "rejected"


class SourceKind(StrEnum):
    DOCUMENT = "document"   # telegram, uncompressed
    PHOTO = "photo"         # telegram, server-side re-encoded
    VIDEO = "video"         # telegram, server-side re-encoded
    FILE = "file"           # viber, uncompressed
    PICTURE = "picture"     # viber, server-side re-encoded

    @property
    def lossless(self) -> bool:
        return self in (SourceKind.DOCUMENT, SourceKind.FILE)


@dataclass(slots=True)
class Group:
    id: int
    name: str
    folder: str


@dataclass(slots=True)
class Person:
    id: int
    name: str
    role: Role = Role.MEMBER
    status: str = "active"
    personal_folder: str | None = None
    group_id: int | None = None
    folder_mode_override: FolderMode | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(slots=True)
class Account:
    id: int
    person_id: int
    platform: Platform
    platform_user_id: str
    username: str | None = None
    chat_id: str | None = None


@dataclass(slots=True)
class IncomingMedia:
    """One media item as handed over by a messenger adapter."""

    platform: Platform
    chat_id: str
    message_id: str
    source_kind: SourceKind
    file_name: str
    #: Opaque handle the adapter later uses to fetch the bytes
    #: (Telegram ``file_id``, Viber media URL).
    file_ref: str
    size: int | None = None
    mime_type: str | None = None
    caption: str | None = None
    media_group_id: str | None = None
    sent_at: datetime | None = None

    @property
    def lossless(self) -> bool:
        return self.source_kind.lossless


@dataclass(slots=True)
class RemoteFile:
    """A file as it exists in the cloud after a successful upload."""

    remote_id: str
    path: str
    size: int | None = None
    #: Hashes the provider reports back, lowercased. Keys: ``sha256``, ``md5``,
    #: ``quickxor``. Used to prove the stored bytes match what we sent.
    hashes: dict[str, str] = field(default_factory=dict)
    web_url: str | None = None


@dataclass(slots=True)
class Quota:
    total: int | None = None
    used: int | None = None

    @property
    def free(self) -> int | None:
        if self.total is None or self.used is None:
            return None
        return self.total - self.used


@dataclass(slots=True)
class LocalFile:
    """A downloaded item on our own disk, with digests computed while streaming."""

    path: Path
    size: int
    sha256: str
    md5: str
    sha1: str = ""
