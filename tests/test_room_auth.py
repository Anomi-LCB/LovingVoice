import time

from app.services.room_auth import RoomTokenManager


def test_room_token_is_scoped_signed_and_expires():
    manager = RoomTokenManager(secret="test-secret", ttl_seconds=60)
    token = manager.issue("123456")

    assert manager.verify("123456", token)
    assert not manager.verify("654321", token)
    assert not manager.verify("123456", token + "tampered")

    expired = RoomTokenManager(secret="test-secret", ttl_seconds=-1).issue("123456")
    time.sleep(0.01)
    assert not manager.verify("123456", expired)


def test_legacy_password_is_supported_without_using_url(monkeypatch):
    monkeypatch.setenv("SPEAKER_PASSWORD", "legacy-secret")
    manager = RoomTokenManager(secret="token-secret")

    assert manager.authenticate(
        "room", {"type": "authenticate", "password": "legacy-secret"}
    )
    assert not manager.authenticate(
        "room", {"type": "authenticate", "password": "wrong"}
    )
