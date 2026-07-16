"""Short-lived room credentials for LovingVoice hosts and audiences."""

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
        self.secret = (configured_secret or secrets.token_urlsafe(32)).encode()
        self.ttl_seconds = ttl_seconds
        self.rooms: dict[str, dict[str, bytes | int]] = {}

    def create_room(self, room_id: str, password: str) -> dict[str, str | int]:
        """Register a user-named room and retain only its password digest."""
        self._prune_expired_rooms()
        if room_id in self.rooms:
            raise ValueError("Room already exists")
        salt = secrets.token_bytes(16)
        password_digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, 200_000
        )
        expires_at = int(time.time()) + self.ttl_seconds
        self.rooms[room_id] = {
            "salt": salt,
            "password_digest": password_digest,
            "expires_at": expires_at,
        }
        return {
            "room_id": room_id,
            "speaker_token": self.issue(room_id, expires_at=expires_at),
            "expires_in_seconds": self.ttl_seconds,
        }

    def update_room(
        self, room_id: str, new_room_id: str, new_password: str
    ) -> dict[str, str | int]:
        """Rename a room and/or replace its audience password."""
        self._prune_expired_rooms()
        room = self.rooms.get(room_id)
        if not room:
            raise KeyError("Room not found")
        if new_room_id != room_id and new_room_id in self.rooms:
            raise ValueError("Room already exists")

        salt = secrets.token_bytes(16)
        password_digest = hashlib.pbkdf2_hmac(
            "sha256", new_password.encode("utf-8"), salt, 200_000
        )
        expires_at = int(room["expires_at"])
        updated_room = {
            "salt": salt,
            "password_digest": password_digest,
            "expires_at": expires_at,
        }
        if new_room_id != room_id:
            del self.rooms[room_id]
        self.rooms[new_room_id] = updated_room
        return {
            "room_id": new_room_id,
            "speaker_token": self.issue(new_room_id, expires_at=expires_at),
            "expires_in_seconds": max(0, expires_at - int(time.time())),
        }

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def issue(self, room_id: str, *, expires_at: int | None = None) -> str:
        payload = {
            "room_id": room_id,
            "expires_at": expires_at or int(time.time()) + self.ttl_seconds,
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

    def verify_speaker_token(self, room_id: str, token: str) -> bool:
        return self.room_exists(room_id) and self.verify(room_id, token)

    def verify_room_password(self, room_id: str, password: str) -> bool:
        self._prune_expired_rooms()
        room = self.rooms.get(room_id)
        if not room or not password:
            return False
        supplied_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.strip().encode("utf-8"),
            room["salt"],
            200_000,
        )
        return hmac.compare_digest(supplied_digest, room["password_digest"])

    def room_exists(self, room_id: str) -> bool:
        self._prune_expired_rooms()
        return room_id in self.rooms

    def _prune_expired_rooms(self) -> None:
        now = int(time.time())
        expired = [
            room_id
            for room_id, room in self.rooms.items()
            if int(room["expires_at"]) < now
        ]
        for room_id in expired:
            del self.rooms[room_id]
