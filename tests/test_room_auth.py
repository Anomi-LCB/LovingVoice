import time

import pytest

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


def test_room_creation_uses_custom_name_and_separate_credentials():
    manager = RoomTokenManager(secret="token-secret")
    room = manager.create_room("주일예배", "My-Secret")

    assert room["room_id"] == "주일예배"
    assert manager.verify_speaker_token(room["room_id"], room["speaker_token"])
    assert manager.verify_room_password(room["room_id"], "My-Secret")
    assert not manager.verify_room_password(room["room_id"], "my-secret")
    assert not manager.verify_room_password(room["room_id"], "WRONG123")
    assert not manager.verify_speaker_token("000000", room["speaker_token"])

    stored_room = manager.rooms[room["room_id"]]
    assert b"My-Secret" not in stored_room.values()

    with pytest.raises(ValueError):
        manager.create_room("주일예배", "Another-Secret")


def test_expired_room_rejects_both_host_and_audience():
    manager = RoomTokenManager(secret="token-secret", ttl_seconds=-1)
    room = manager.create_room("expired-room", "Expired-Secret")

    assert not manager.room_exists(room["room_id"])
    assert not manager.verify_room_password(room["room_id"], "Expired-Secret")
    assert not manager.verify_speaker_token(room["room_id"], room["speaker_token"])


def test_room_name_and_password_can_be_updated_by_host_flow():
    manager = RoomTokenManager(secret="token-secret")
    original = manager.create_room("기존방", "Old-Secret")

    updated = manager.update_room("기존방", "새로운방", "New-Secret")

    assert not manager.room_exists("기존방")
    assert manager.room_exists("새로운방")
    assert not manager.verify_speaker_token("기존방", original["speaker_token"])
    assert manager.verify_speaker_token("새로운방", updated["speaker_token"])
    assert not manager.verify_room_password("새로운방", "Old-Secret")
    assert manager.verify_room_password("새로운방", "New-Secret")


def test_room_update_rejects_an_existing_new_name():
    manager = RoomTokenManager(secret="token-secret")
    manager.create_room("first", "First-Secret")
    manager.create_room("second", "Second-Secret")

    with pytest.raises(ValueError):
        manager.update_room("first", "second", "Updated-Secret")
