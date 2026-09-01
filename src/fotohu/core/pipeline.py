"""The upload pipeline: one queued message -> one verified file in the cloud.

The ordering here is deliberate and is the safety property of the whole project:
a chat message becomes eligible for deletion only after the bytes are in the
cloud *and* the provider's own digest matches what we sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..db.repo import Repo
from ..messengers.base import MessengerAdapter
from ..services.settings import Settings
from ..storage.base import StorageBackend
from . import exif, naming
from .errors import FileTooLarge, IntegrityError
from .models import Group, LocalFile, Person, PhotoPolicy, RemoteFile, UploadState

log = logging.getLogger(__name__)

MAX_NAME_COLLISION_ATTEMPTS = 50


@dataclass(slots=True)
class UploadOutcome:
    state: UploadState
    remote_path: str | None = None
    remote_file: RemoteFile | None = None
    verified: bool | None = None
    duplicate_of: str | None = None
    reason: str | None = None


def resolve_destination(
    *,
    person: Person,
    group: Group | None,
    settings: Settings,
    root_folder: str,
    taken_at: datetime,
    original_name: str,
    compressed: bool,
) -> tuple[str, str]:
    """Return ``(remote_dir, filename)`` for one file. Pure — heavily unit-tested."""
    owner = naming.owner_segment(person, group, settings.folder_mode)
    ctx = naming.build_context(
        root=root_folder,
        owner=owner,
        taken_at=taken_at,
        filename=naming.sanitize_filename(original_name),
        person=person,
        group=group,
        compressed=compressed,
    )
    return (
        naming.build_remote_dir(settings.dir_template, ctx),
        naming.build_remote_filename(settings.file_template, ctx),
    )


async def process_upload(
    *,
    record: dict,
    repo: Repo,
    adapter: MessengerAdapter,
    backend: StorageBackend,
    storage_account_id: int,
    root_folder: str,
    settings: Settings,
    person: Person,
    group: Group | None,
    temp_dir: Path,
) -> UploadOutcome:
    upload_id = record["id"]
    compressed = not record["lossless"]

    if compressed and settings.photo_policy == PhotoPolicy.REJECT:
        return UploadOutcome(state=UploadState.REJECTED, reason="compressed")

    # --- 1. pull the bytes down, hashing as we stream -------------------------
    limit = min(
        settings.max_file_mb * 1024 * 1024,
        adapter.download_limit or settings.max_file_mb * 1024 * 1024,
    )
    temp_path = temp_dir / f"upload-{upload_id}-{naming.sanitize_filename(record['file_name'])}"
    local: LocalFile = await adapter.download(record["remote_file_id"], temp_path, limit)

    try:
        # --- 2. decide where it goes ------------------------------------------
        sent_at = _parse_ts(record.get("taken_at"))
        taken_at, date_source = exif.resolve_taken_at(
            local.path, sent_at, prefer_exif=settings.prefer_exif_date
        )
        remote_dir, filename = resolve_destination(
            person=person,
            group=group,
            settings=settings,
            root_folder=root_folder,
            taken_at=taken_at,
            original_name=record["file_name"],
            compressed=compressed and settings.photo_policy == PhotoPolicy.SAVE_MARKED,
        )
        await repo.update_upload(
            upload_id,
            size=local.size,
            sha256=local.sha256,
            md5=local.md5,
            taken_at=taken_at.strftime("%Y-%m-%d %H:%M:%S"),
            date_source=date_source,
        )

        # --- 3. skip anything we already have ---------------------------------
        if settings.dedupe_enabled:
            duplicate = await repo.find_duplicate(local.sha256, remote_dir)
            if duplicate and duplicate["id"] != upload_id:
                return UploadOutcome(
                    state=UploadState.SKIPPED_DUP, duplicate_of=duplicate["remote_path"]
                )

        # --- 4. never overwrite: find a free name ------------------------------
        free_name = await _free_filename(backend, remote_dir, filename, local)
        if free_name is None:
            return UploadOutcome(
                state=UploadState.SKIPPED_DUP, duplicate_of=f"{remote_dir}/{filename}"
            )

        # --- 5. upload and verify ---------------------------------------------
        remote = await backend.upload(local, remote_dir, free_name)
        verified = backend.verify(local, remote)
        if settings.verify_hashes and verified is False:
            raise IntegrityError(
                f"digest mismatch for {remote.path}: the cloud copy differs from what we sent"
            )

        return UploadOutcome(
            state=UploadState.DONE,
            remote_path=remote.path,
            remote_file=remote,
            verified=bool(verified),
        )
    finally:
        temp_path.unlink(missing_ok=True)


async def _free_filename(
    backend: StorageBackend, remote_dir: str, filename: str, local: LocalFile
) -> str | None:
    """Return a name nothing occupies, or ``None`` if the identical file is there.

    Suffixing rather than overwriting is deliberate: two people photographing the
    same scene both produce ``IMG_0001.JPG``, and losing one silently would be the
    worst possible failure for a photo archive.
    """
    for attempt in range(1, MAX_NAME_COLLISION_ATTEMPTS + 1):
        candidate = naming.dedupe_filename(filename, attempt)
        existing = await backend.exists(remote_dir, candidate)
        if existing is None:
            return candidate
        if backend.verify(local, existing):
            return None  # byte-identical: already archived, nothing to do
    raise RuntimeError(f"could not find a free name for {filename} in {remote_dir}")


def purge_deadline(settings: Settings, now: datetime | None = None) -> datetime | None:
    if not settings.purge_enabled:
        return None
    return (now or datetime.now()) + timedelta(hours=max(0, settings.purge_after_hours))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


__all__ = [
    "UploadOutcome",
    "process_upload",
    "resolve_destination",
    "purge_deadline",
    "FileTooLarge",
]
