"""Exception hierarchy. Anything user-facing carries a translated message key."""

from __future__ import annotations


class FotoHuError(Exception):
    """Base class."""


class ConfigError(FotoHuError):
    pass


class NotAuthorized(FotoHuError):
    pass


class StorageError(FotoHuError):
    """A cloud backend refused or failed."""


class StorageAuthError(StorageError):
    """Credentials are missing, expired or were revoked — needs re-linking."""


class QuotaExceeded(StorageError):
    pass


class RetryableError(FotoHuError):
    """Transient; the worker should back off and try again.

    ``retry_after`` is how long the other side asked us to wait, in seconds,
    when it bothered to say — Telegram's flood control always does. Waiting
    exactly that long beats guessing, and beats giving up.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FileTooLarge(FotoHuError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"file is {size} bytes, limit is {limit}")
        self.size = size
        self.limit = limit


class DownloadError(FotoHuError):
    pass


class IntegrityError(FotoHuError):
    """The cloud copy's digest does not match what we uploaded."""
