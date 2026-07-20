from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main


class FakeSoloHub:
    available = True

    def __init__(self):
        self.prepare_calls = []
        self.feed_calls = []
        self.closed = []

    async def prepare(self, room_id, source_id, languages):
        self.prepare_calls.append((room_id, source_id, tuple(languages)))

    async def feed(self, room_id, source_id, audio, languages):
        self.feed_calls.append((room_id, source_id, audio, tuple(languages)))

    async def close_speaker(self, room_id, source_id):
        self.closed.append((room_id, source_id))

    async def close(self):
        return None


class FakeSoloManager:
    def __init__(self):
        self.audiences = []
        self.removed = []
        self.subtitle_history = {}

    async def add_audience(
        self, room_id, language, websocket, *, accept, audio_transport
    ):
        self.audiences.append((room_id, language, websocket, audio_transport))

    def remove_audience(
        self, room_id, language, websocket, *, preserve_transport=False
    ):
        self.removed.append((room_id, language, websocket))

    async def queue_json_to_room(self, room_id, event):
        return None

    async def close_realtime_fanout(self):
        return None


def test_solo_websocket_streams_audio_and_switches_language(monkeypatch):
    hub = FakeSoloHub()
    manager = FakeSoloManager()
    monkeypatch.setattr(main, "realtime_hub", hub)
    monkeypatch.setattr(main, "manager", manager)

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/solo/en-US?transport=pcm16-v1") as socket:
            authenticated = socket.receive_json()
            assert authenticated["type"] == "authenticated"
            assert authenticated["mode"] == "solo"
            assert authenticated["language"] == "en-US"

            socket.send_bytes(b"\x00\x00" * 240)
            socket.send_json({"type": "quality_ping", "sent_at": 123.5})
            assert socket.receive_json() == {
                "type": "quality_pong",
                "sent_at": 123.5,
            }

            socket.send_json({"type": "switch_language", "language": "ko-KR"})
            assert socket.receive_json() == {
                "type": "language_switched",
                "language": "ko-KR",
            }

    assert [call[2] for call in hub.prepare_calls] == [("en-US",), ("ko-KR",)]
    assert hub.feed_calls[0][2] == b"\x00\x00" * 240
    assert hub.feed_calls[0][3] == ("en-US",)
    assert hub.closed
    assert [item[1] for item in manager.audiences] == ["en-US", "ko-KR"]


def test_solo_websocket_rejects_unsupported_language(monkeypatch):
    monkeypatch.setattr(main, "realtime_hub", FakeSoloHub())
    monkeypatch.setattr(main, "manager", FakeSoloManager())

    with TestClient(main.app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/solo/not-a-language") as socket:
                socket.receive_json()
    assert exc.value.code == 4004


def test_solo_ui_has_single_device_controls_and_language_swap():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")

    assert "1인 동시통역" in html
    assert 'id="solo_source_lang"' in html
    assert 'id="solo_target_lang"' in html
    assert 'onclick="swapSoloLanguages()"' in html
    assert "/ws/solo/${encodeURIComponent(soloTargetLanguage)}" in html
    assert "echoCancellation: true" in html
    assert "while (history.childElementCount > AUDIENCE_HISTORY_MAX_ITEMS)" in html

    playback_function = html.split("function playPcm16Bytes(", 1)[1].split(
        "async function startSpeaker()", 1
    )[0]
    assert "kind === 'source'" in playback_function
    assert "SOURCE_ASSIST_MAX_QUEUE_SECONDS" in playback_function
    translation_path = playback_function.split("kind === 'source'", 1)[0]
    assert "source.stop()" not in translation_path
