"""Encryption of stored OAuth credentials, and signed single-use tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret_key: str) -> Fernet:
    """Accept either a real Fernet key or any passphrase (hashed into one)."""
    raw = secret_key.encode()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return Fernet(raw)
    except Exception:  # noqa: BLE001 - not a Fernet key, fall through to hashing
        pass
    derived = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(derived)


def encrypt_json(secret_key: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return _fernet(secret_key).encrypt(blob).decode()


def decrypt_json(secret_key: str, token: str) -> dict[str, Any]:
    try:
        return json.loads(_fernet(secret_key).decrypt(token.encode()))
    except InvalidToken as exc:
        raise ValueError("cannot decrypt credentials — is FOTOHU_SECRET_KEY the same?") from exc


_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes


def new_invite_code(length: int = 8) -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(length))


def new_state_token() -> str:
    return secrets.token_urlsafe(32)


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for OAuth 2.0 PKCE (S256)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
