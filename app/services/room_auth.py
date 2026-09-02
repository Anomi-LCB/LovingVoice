"""Short-lived, room-scoped speaker credentials for LovingVoice."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time


class RoomTokenManager:
    def __init__(self, secret: str | None = None, ttl_seconds: int = 12 * 60 * 60):
        configured_secret = secret or os.getenv("ROOM_SIGNING_SECRET")
        legacy_password = os.getenv("SPEAKER_PASSWORD", "")
        self.secret = (configured_secret or legacy_password or secrets.token_urlsafe(32)).encode()
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def create_room_id() -> str:
        """Return an easy-to-share six digit room code."""
        return f"{secrets.randbelow(900_000) + 100_000:06d}"

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def issue(self, room_id: str) -> str:
        payload = {
            "room_id": room_id,
            "expires_at": int(time.time()) + self.ttl_seconds,
            "nonce": secrets.token_hex(8),
        }
        encoded_payload = self._encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self.secret, encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{encoded_payload}.{self._encode(signature)}"

    def verify(self, room_id: str, token: str) -> bool:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            expected = hmac.new(
                self.secret, encoded_payload.encode("ascii"), hashlib.sha256
            ).digest()
            supplied = self._decode(encoded_signature)
            if not hmac.compare_digest(expected, supplied):
                return False
            payload = json.loads(self._decode(encoded_payload))
            return (
                payload.get("room_id") == room_id
                and int(payload.get("expires_at", 0)) >= int(time.time())
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def authenticate(self, room_id: str, message: dict) -> bool:
        token = message.get("token", "")
        if token and self.verify(room_id, token):
            return True

        # Transitional compatibility for manually entered room codes. The
        # password travels inside TLS/WebSocket data, never in the URL. There
        # is deliberately no built-in fallback password: operators must set
        # SPEAKER_PASSWORD explicitly while this legacy path remains enabled.
        configured_password = os.getenv("SPEAKER_PASSWORD", "")
        supplied_password = str(message.get("password", ""))
        return bool(configured_password) and hmac.compare_digest(
            supplied_password, configured_password
        )
