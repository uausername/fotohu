"""Credential handling, webhook authenticity, OAuth state and log redaction."""

from __future__ import annotations

import pytest

from fotohu.core.crypto import decrypt_json, encrypt_json, new_invite_code, pkce_pair
from fotohu.logging import redact
from fotohu.messengers.viber import verify_signature


class TestCredentialEncryption:
    def test_round_trip(self):
        payload = {"access_token": "abc", "refresh_token": "def", "expires_at": 123.0}
        blob = encrypt_json("secret", payload)
        assert "abc" not in blob and "def" not in blob
        assert decrypt_json("secret", blob) == payload

    def test_a_different_key_cannot_read_it(self):
        blob = encrypt_json("secret-one", {"access_token": "abc"})
        with pytest.raises(ValueError):
            decrypt_json("secret-two", blob)

    def test_a_real_fernet_key_is_accepted_as_is(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        assert decrypt_json(key, encrypt_json(key, {"a": 1})) == {"a": 1}

    async def test_tokens_are_never_stored_in_the_clear(self, ctx):
        account_id = await ctx.repo.create_storage_account("onedrive", "OneDrive")
        await ctx.storage.save_credentials(
            account_id, {"refresh_token": "super-secret-value"}
        )
        record = await ctx.repo.get_storage_account(account_id)
        assert "super-secret-value" not in record["credentials_enc"]

        backend = await ctx.storage.build(record)
        assert backend.credentials["refresh_token"] == "super-secret-value"


class TestViberSignature:
    TOKEN = "4453b6ac1s345678-e02c9d3a9c1b-a1b2c3d4e5f6"

    def test_a_correct_signature_passes(self):
        import hashlib
        import hmac

        body = b'{"event":"message"}'
        signature = hmac.new(self.TOKEN.encode(), body, hashlib.sha256).hexdigest()
        assert verify_signature(self.TOKEN, body, signature)

    def test_a_tampered_body_fails(self):
        import hashlib
        import hmac

        signature = hmac.new(self.TOKEN.encode(), b"original", hashlib.sha256).hexdigest()
        assert not verify_signature(self.TOKEN, b"tampered", signature)

    @pytest.mark.parametrize("signature", [None, "", "deadbeef", "not-hex"])
    def test_missing_or_bogus_signatures_fail(self, signature):
        assert not verify_signature(self.TOKEN, b"{}", signature)


class TestOAuthState:
    async def test_a_state_can_be_redeemed_only_once(self, ctx):
        await ctx.repo.create_oauth_state("st-1", "gdrive", None, "verifier", {"account_id": 1})
        assert await ctx.repo.consume_oauth_state("st-1") is not None
        assert await ctx.repo.consume_oauth_state("st-1") is None

    async def test_an_expired_state_is_rejected(self, ctx):
        await ctx.repo.create_oauth_state(
            "st-2", "gdrive", None, "v", {"account_id": 1}, ttl_minutes=-1
        )
        assert await ctx.repo.consume_oauth_state("st-2") is None

    async def test_an_unknown_state_is_rejected(self, ctx):
        assert await ctx.repo.consume_oauth_state("never-issued") is None

    def test_pkce_challenge_matches_its_verifier(self):
        import base64
        import hashlib

        verifier, challenge = pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected


class TestLogRedaction:
    @pytest.mark.parametrize(
        "line",
        [
            "calling https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe",
            'response {"access_token": "ya29.a0AfH6SMBxxxxxxxxxxxxxxxx"}',
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            'client_secret=GOCSPX-abcdefghijklmnop',
        ],
    )
    def test_secrets_do_not_survive_redaction(self, line):
        cleaned = redact(line)
        assert "redacted" in cleaned
        for secret in ("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", "ya29.a0AfH6SMBx",
                       "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "GOCSPX-abcdefghijklmnop"):
            assert secret not in cleaned

    def test_ordinary_lines_are_untouched(self):
        line = "uploaded FotoHu/dmitrii/2026/2026-03/IMG_0042.JPG (4.1 MB)"
        assert redact(line) == line


class TestInviteCodes:
    def test_codes_avoid_look_alike_characters(self):
        for _ in range(200):
            code = new_invite_code()
            assert len(code) == 8
            assert not set(code) & set("01IO")
