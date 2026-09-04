"""Environment-level configuration.

Only infrastructure and secrets live here. Everything an admin can change at
runtime (storage backend, folder layout, retention, quality policy) lives in the
``settings`` table and is reached through :mod:`fotohu.services.settings`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class TelegramConfig:
    token: str | None = None
    #: Base URL of the Bot API. Point at a self-hosted ``telegram-bot-api`` to
    #: lift the 20 MB download ceiling to 2 GB.
    api_base: str | None = None
    #: When the local API server shares a volume with us, ``getFile`` returns an
    #: absolute path on disk and we can skip the HTTP download entirely.
    local_mode: bool = False
    use_webhook: bool = False
    webhook_secret: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)


@dataclass(frozen=True)
class ViberConfig:
    token: str | None = None
    bot_name: str = "FotoHu"
    avatar: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    temp_dir: Path
    secret_key: str
    public_url: str | None
    bootstrap_token: str | None
    log_level: str
    log_file: str | None
    language: str
    http_host: str
    http_port: int
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    viber: ViberConfig = field(default_factory=ViberConfig)
    rclone_binary: str = "rclone"
    rclone_config: str | None = None
    max_upload_bytes: int = 2 * 1024**3
    worker_concurrency: int = 2

    @property
    def telegram_webhook_path(self) -> str:
        return "/webhook/telegram"

    @property
    def viber_webhook_path(self) -> str:
        return "/webhook/viber"

    @property
    def oauth_redirect_path(self) -> str:
        return "/oauth/callback"

    @property
    def oauth_redirect_uri(self) -> str | None:
        if not self.public_url:
            return None
        return self.public_url.rstrip("/") + self.oauth_redirect_path

    @property
    def needs_public_access(self) -> bool:
        """True when the HTTP surface has to be reachable from the internet.

        Only Viber's webhook, an opt-in Telegram webhook and the OAuth callback
        need that. ``/health`` is served regardless — see :mod:`fotohu.__main__`.
        """
        return bool(self.viber.enabled or self.telegram.use_webhook or self.public_url)


def load_config(env_file: str | os.PathLike[str] | None = ".env") -> Config:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file)

    data_dir = Path(os.getenv("FOTOHU_DATA_DIR", "./data")).expanduser()
    secret_key = os.getenv("FOTOHU_SECRET_KEY", "").strip()
    if not secret_key:
        raise RuntimeError(
            "FOTOHU_SECRET_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )

    public_url = os.getenv("FOTOHU_PUBLIC_URL", "").strip() or None

    return Config(
        data_dir=data_dir,
        db_path=Path(os.getenv("FOTOHU_DB_PATH", str(data_dir / "fotohu.sqlite3"))),
        temp_dir=Path(os.getenv("FOTOHU_TEMP_DIR", str(data_dir / "tmp"))),
        secret_key=secret_key,
        public_url=public_url,
        bootstrap_token=os.getenv("FOTOHU_BOOTSTRAP_TOKEN", "").strip() or None,
        log_level=os.getenv("FOTOHU_LOG_LEVEL", "INFO").upper(),
        log_file=os.getenv("FOTOHU_LOG_FILE", "").strip() or None,
        language=os.getenv("FOTOHU_LANGUAGE", "ru").lower(),
        # Loopback by default: the only always-on route is /health, and a bare
        # metal install should not open a port on the LAN just to have it. The
        # Docker image sets 0.0.0.0, because there the container's own namespace
        # is the boundary and compose publishes the port to the host's loopback.
        http_host=os.getenv("FOTOHU_HTTP_HOST", "127.0.0.1"),
        http_port=_int("FOTOHU_HTTP_PORT", 8080),
        telegram=TelegramConfig(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
            api_base=os.getenv("TELEGRAM_API_BASE", "").strip() or None,
            local_mode=_bool("TELEGRAM_API_LOCAL_MODE"),
            use_webhook=_bool("TELEGRAM_USE_WEBHOOK"),
            webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None,
        ),
        viber=ViberConfig(
            token=os.getenv("VIBER_BOT_TOKEN", "").strip() or None,
            bot_name=os.getenv("VIBER_BOT_NAME", "FotoHu"),
            avatar=os.getenv("VIBER_BOT_AVATAR", "").strip() or None,
        ),
        rclone_binary=os.getenv("RCLONE_BINARY", "rclone"),
        rclone_config=os.getenv("RCLONE_CONFIG", "").strip() or None,
        max_upload_bytes=_int("FOTOHU_MAX_UPLOAD_BYTES", 2 * 1024**3),
        worker_concurrency=_int("FOTOHU_WORKER_CONCURRENCY", 2),
    )
