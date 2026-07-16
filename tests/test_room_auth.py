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


def test_room_creation_generates_separate_host_and_audience_credentials():
    manager = RoomTokenManager(secret="token-secret")
    room = manager.create_room()

    assert len(room["room_id"]) == 6
    assert len(room["room_password"]) == 8
    assert manager.verify_speaker_token(room["room_id"], room["speaker_token"])
    assert manager.verify_room_password(room["room_id"], room["room_password"])
    assert manager.verify_room_password(
        room["room_id"], room["room_password"].lower()
    )
    assert not manager.verify_room_password(room["room_id"], "WRONG123")
    assert not manager.verify_speaker_token("000000", room["speaker_token"])

    stored_room = manager.rooms[room["room_id"]]
    assert room["room_password"].encode() not in stored_room.values()


def test_expired_room_rejects_both_host_and_audience():
    manager = RoomTokenManager(secret="token-secret", ttl_seconds=-1)
    room = manager.create_room()

    assert not manager.room_exists(room["room_id"])
    assert not manager.verify_room_password(room["room_id"], room["room_password"])
    assert not manager.verify_speaker_token(room["room_id"], room["speaker_token"])
