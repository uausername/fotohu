"""Shared OAuth 2.0 machinery for the token-based cloud backends.

Both OneDrive and Google Drive use authorization-code + PKCE with a redirect
back to our own web server, so the refresh/expiry bookkeeping lives here once.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..core.crypto import pkce_pair
from ..core.errors import RetryableError, StorageAuthError, StorageError

log = logging.getLogger(__name__)

#: Refresh this long before the token actually dies, so a slow upload that
#: starts just under the wire does not fail halfway through.
EXPIRY_SKEW_SECONDS = 120


class OAuthMixin:
    """Expects the host class to define ``token_url``, ``client_id_env`` and
    ``client_secret_env``, and to own a ``credentials`` dict."""

    token_url: str
    client_id_env: str
    client_secret_env: str

    credentials: dict[str, Any]
    credentials_dirty: bool

    # ---------------------------------------------------------------- app creds

    @classmethod
    def _client_id(cls) -> str:
        value = os.getenv(cls.client_id_env, "").strip()
        if not value:
            raise StorageError(f"{cls.client_id_env} is not set")
        return value

    @classmethod
    def _client_secret(cls) -> str | None:
        return os.getenv(cls.client_secret_env, "").strip() or None

    @staticmethod
    def _pkce() -> tuple[str, str]:
        return pkce_pair()

    # ------------------------------------------------------------ code exchange

    @classmethod
    async def _exchange_code(
        cls, *, code: str, verifier: str | None, redirect_uri: str, scope: str | None = None
    ) -> dict[str, Any]:
        payload = {
            "client_id": cls._client_id(),
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if verifier:
            payload["code_verifier"] = verifier
        if secret := cls._client_secret():
            payload["client_secret"] = secret
        if scope:
            payload["scope"] = scope

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(cls.token_url, data=payload)
        if response.status_code >= 400:
            raise StorageAuthError(f"token exchange failed: {response.text[:400]}")

        tokens = response.json()
        if not tokens.get("refresh_token"):
            raise StorageAuthError(
                "the provider returned no refresh_token — the app registration is "
                "probably missing offline access, so the link would break in an hour"
            )
        return cls._store_tokens(tokens)

    @staticmethod
    def _store_tokens(tokens: dict[str, Any], previous: dict[str, Any] | None = None) -> dict:
        """Normalise a token response into what we persist."""
        merged = dict(previous or {})
        merged["access_token"] = tokens.get("access_token")
        # A refresh response often omits refresh_token, meaning "keep the old one".
        if tokens.get("refresh_token"):
            merged["refresh_token"] = tokens["refresh_token"]
        merged["expires_at"] = time.time() + float(tokens.get("expires_in", 3600))
        if tokens.get("scope"):
            merged["scope"] = tokens["scope"]
        return merged

    # --------------------------------------------------------- runtime refresh

    def _force_refresh(self) -> None:
        self.credentials["expires_at"] = 0

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        creds = self.credentials
        expires_at = float(creds.get("expires_at") or 0)
        if creds.get("access_token") and expires_at - EXPIRY_SKEW_SECONDS > time.time():
            return creds["access_token"]

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            raise StorageAuthError("no refresh token stored — link the account again")

        payload = {
            "client_id": self._client_id(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if secret := self._client_secret():
            payload["client_secret"] = secret

        response = await client.post(self.token_url, data=payload)
        if response.status_code >= 400:
            body = response.text[:400]
            if response.status_code >= 500 or response.status_code == 429:
                raise RetryableError(f"token endpoint {response.status_code}: {body}")
            raise StorageAuthError(f"refresh failed ({response.status_code}): {body}")

        self.credentials = self._store_tokens(response.json(), previous=creds)
        # Tells the registry to write the rotated refresh token back to the DB;
        # Google rotates it on some flows and losing it means re-linking.
        self.credentials_dirty = True
        log.info("refreshed access token for %s", type(self).__name__)
        return self.credentials["access_token"]
