"""Streaming download helpers.

Files are never held in memory: we stream to a temp file on disk while computing
SHA-256 and MD5 in the same pass, so integrity checking is free.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from .errors import FileTooLarge
from .models import LocalFile

log = logging.getLogger(__name__)

CHUNK = 1024 * 256


async def stream_to_file(
    chunks: AsyncIterator[bytes],
    dest: Path,
    *,
    size_limit: int | None = None,
) -> LocalFile:
    dest.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256()
    md5 = hashlib.md5()  # noqa: S324 - not for security; Google Drive reports MD5
    sha1 = hashlib.sha1()  # noqa: S324 - not for security; OneDrive reports SHA-1
    total = 0

    try:
        with dest.open("wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if size_limit is not None and total > size_limit:
                    raise FileTooLarge(total, size_limit)
                sha.update(chunk)
                md5.update(chunk)
                sha1.update(chunk)
                fh.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    return LocalFile(
        path=dest,
        size=total,
        sha256=sha.hexdigest(),
        md5=md5.hexdigest(),
        sha1=sha1.hexdigest(),
    )


def digest_file(path: Path) -> LocalFile:
    """Digest a file already on disk (the local Bot API server case)."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()  # noqa: S324
    sha1 = hashlib.sha1()  # noqa: S324
    total = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            total += len(chunk)
            sha.update(chunk)
            md5.update(chunk)
            sha1.update(chunk)
    return LocalFile(
        path=path,
        size=total,
        sha256=sha.hexdigest(),
        md5=md5.hexdigest(),
        sha1=sha1.hexdigest(),
    )


def copy_into(src: Path, dest: Path) -> LocalFile:
    """Copy a local-API-server file into our temp dir so we own its lifetime."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return digest_file(dest)
