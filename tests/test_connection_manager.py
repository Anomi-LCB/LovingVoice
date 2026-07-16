import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.connection import ConnectionManager


class FakeClientState:
    name = "CONNECTED"


class FakeAudience:
    def __init__(self):
        self.client_state = FakeClientState()
        self.accepted = False
        self.messages = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.messages.append(message)


def test_default_subtitle_history_keeps_twenty_completed_captions(monkeypatch):
    monkeypatch.delenv("SUBTITLE_HISTORY_LIMIT", raising=False)

    manager = ConnectionManager()

    assert manager.history_limit == 20


@pytest.mark.asyncio
async def test_speaker_connections_are_not_artificially_limited():
    manager = ConnectionManager()
    speakers = [object() for _ in range(25)]

    results = [await manager.add_speaker("room", speaker) for speaker in speakers]

    assert all(results)
    assert manager.active_speakers["room"] == speakers


@pytest.mark.asyncio
async def test_same_language_audiences_share_one_language_channel():
    manager = ConnectionManager()
    english_audience = [FakeAudience() for _ in range(250)]
    japanese_audience = [FakeAudience() for _ in range(75)]

    for audience in english_audience:
        await manager.add_audience("room", "en-US", audience)
    for audience in japanese_audience:
        await manager.add_audience("room", "ja-JP", audience)

    assert set(manager.audience_languages("room")) == {"en-US", "ja-JP"}
    assert manager.room_status("room")["audience_count"] == 325
    assert manager.room_status("room")["audience_by_language"] == {
        "en-US": 250,
        "ja-JP": 75,
    }

    message = {"type": "translation_delta", "text": "Shared output"}
    await manager.broadcast_json_to_language("room", "en-US", message)

    assert all(audience.messages == [message] for audience in english_audience)
    assert all(audience.messages == [] for audience in japanese_audience)


@pytest.mark.asyncio
async def test_completed_subtitles_are_bounded_and_available_to_late_joiners(monkeypatch):
    monkeypatch.setenv("SUBTITLE_HISTORY_LIMIT", "10")
    manager = ConnectionManager()

    for index in range(12):
        await manager.record_subtitle(
            "room", "en-US", "speaker", f"Caption {index}"
        )

    snapshot = manager.get_subtitle_history("room", "en-US")
    assert len(snapshot) == 10
    assert snapshot[0]["text"] == "Caption 2"
    assert snapshot[-1]["text"] == "Caption 11"
    assert manager.room_status("room")["history_available_by_language"] == {
        "en-US": 10
    }


@pytest.mark.asyncio
async def test_late_joiners_receive_only_recent_captions(monkeypatch):
    monkeypatch.setenv("SUBTITLE_HISTORY_MAX_AGE_SECONDS", "30")
    manager = ConnectionManager()

    await manager.record_subtitle("room", "ko-KR", "speaker", "old caption")
    await manager.record_subtitle("room", "ko-KR", "speaker", "recent caption")
    manager.subtitle_history["room"]["ko-KR"][0]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=31)
    ).isoformat()

    snapshot = manager.get_subtitle_history("room", "ko-KR")

    assert [item["text"] for item in snapshot] == ["recent caption"]


@pytest.mark.asyncio
async def test_late_join_snapshot_respects_payload_budget(monkeypatch):
    monkeypatch.setenv("SUBTITLE_HISTORY_MAX_BYTES", "1024")
    manager = ConnectionManager()

    for index in range(10):
        await manager.record_subtitle(
            "room", "en-US", "speaker", f"{index}:" + "x" * 600
        )

    snapshot = manager.get_subtitle_history("room", "en-US")
    serialized = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

    assert 0 < len(snapshot) < 10
    assert len(serialized.encode("utf-8")) <= 1024
    assert snapshot[-1]["text"].startswith("9:")


@pytest.mark.asyncio
async def test_renamed_room_keeps_live_channel_and_speaker_events():
    manager = ConnectionManager()
    speaker = FakeAudience()
    existing_audience = FakeAudience()
    new_audience = FakeAudience()

    await manager.add_speaker("original", speaker)
    await manager.add_audience("original", "ko-KR", existing_audience)
    manager.register_room_alias("original", "renamed")
    await manager.add_audience("renamed", "en-US", new_audience)

    message = {"type": "transcript_delta", "text": "speaker caption"}
    await manager.broadcast_json_to_room("original", message)

    assert speaker.messages == [message]
    assert existing_audience.messages == [message]
    assert new_audience.messages == [message]
    assert manager.room_status("renamed")["audience_count"] == 2
