"""What the pipeline needs from a messenger, and nothing more.

Keeping this surface tiny is what lets the upload worker and the purge worker
stay completely platform-agnostic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import LocalFile, Platform


@dataclass(slots=True)
class DeleteResult:
    deleted: list[str] = field(default_factory=list)
    #: message id -> why it could not be removed (shown in the admin panel).
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


class MessengerAdapter(abc.ABC):
    """One messenger, as seen by the workers."""

    platform: Platform

    #: False for Viber: its bot API simply has no delete method, so the purge
    #: worker must not keep retrying and must say so in the admin panel.
    supports_deletion: bool = False

    #: How old a message may be and still be deletable. Telegram enforces 48 h
    #: server-side; ``None`` means "no known limit".
    delete_window_hours: int | None = None

    #: Largest file we can pull back out of the messenger.
    download_limit: int | None = None

    @abc.abstractmethod
    async def download(
        self, file_ref: str, dest: Path, size_limit: int | None = None
    ) -> LocalFile:
        """Fetch the media to ``dest``, hashing as it streams."""

    @abc.abstractmethod
    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str | None:
        """Send a plain message; return its id so it can be purged later."""

    async def delete_messages(self, chat_id: str, message_ids: list[str]) -> DeleteResult:
        return DeleteResult(
            failed={mid: "deletion is not supported by this messenger" for mid in message_ids}
        )

    async def close(self) -> None:
        return None
