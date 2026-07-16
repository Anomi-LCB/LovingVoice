import base64

import pytest

from app.services.realtime_translation import LANGUAGE_CODES, RealtimeTranslationHub


@pytest.mark.asyncio
async def test_routes_realtime_audio_and_translation_to_target_language():
    room_events = []
    language_events = []

    async def on_room(room, event):
        room_events.append((room, event))

    async def on_language(room, language, event):
        language_events.append((room, language, event))

    hub = RealtimeTranslationHub(
        on_room,
        on_language,
        api_key="test-key",
    )
    hub.sessions[("7777", "speaker-a", "en-US")] = object()

    audio = base64.b64encode(b"\x00\x01").decode("ascii")
    await hub._route_event(
        "7777",
        "speaker-a",
        "en-US",
        {"type": "session.output_audio.delta", "delta": audio},
    )
    await hub._route_event(
        "7777",
        "speaker-a",
        "en-US",
        {"type": "session.output_transcript.delta", "delta": "Hello"},
    )

    assert language_events[0][2] == {
        "type": "audio_delta",
        "audio": audio,
        "format": "pcm16",
        "sample_rate": 24000,
        "source_id": "speaker-a",
    }
    assert language_events[1][2] == {
        "type": "translation_delta",
        "text": "Hello",
        "source_id": "speaker-a",
    }
    assert room_events == []


@pytest.mark.asyncio
async def test_only_primary_language_publishes_source_transcript():
    room_events = []

    async def on_room(room, event):
        room_events.append((room, event))

    async def on_language(room, language, event):
        pass

    hub = RealtimeTranslationHub(on_room, on_language, api_key="test-key")
    hub.sessions[("room", "speaker-a", "en-US")] = object()
    hub.sessions[("room", "speaker-a", "ja-JP")] = object()

    event = {"type": "session.input_transcript.delta", "delta": "안녕"}
    await hub._route_event("room", "speaker-a", "ja-JP", event)
    await hub._route_event("room", "speaker-a", "en-US", event)

    assert room_events == [
        (
            "room",
            {
                "type": "transcript_delta",
                "text": "안녕",
                "source_id": "speaker-a",
            },
        )
    ]


def test_supported_ui_languages_map_to_openai_language_codes():
    for language in (
        "ko-KR",
        "en-US",
        "zh-TW",
        "zh-CN",
        "ja-JP",
        "vi-VN",
        "ta-IN",
        "ne-NP",
        "mn-MN",
        "my-MM",
        "km-KH",
    ):
        assert language in LANGUAGE_CODES


@pytest.mark.asyncio
async def test_listener_count_never_multiplies_openai_routes(monkeypatch):
    created = []

    class FakeSession:
        def __init__(self, target_language, on_event, **kwargs):
            self.target_language = target_language
            created.append(self)

        def start(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(
        "app.services.realtime_translation.OpenAITranslationSession", FakeSession
    )

    async def ignore(*args):
        pass

    hub = RealtimeTranslationHub(ignore, ignore, api_key="test-key")
    one_thousand_english_listeners = ["en-US"] * 1000

    await hub._sync_languages(
        "room", "source-channel", one_thousand_english_listeners
    )
    await hub._sync_languages(
        "room", "source-channel", one_thousand_english_listeners
    )

    assert hub.active_session_count == 1
    assert len(created) == 1
    assert hub.route_status("room") == [
        {"source_id": "source-channel", "language": "en-US"}
    ]


@pytest.mark.asyncio
async def test_completed_translation_is_recorded_before_done_event():
    language_events = []
    completed = []

    async def ignore_room(*args):
        pass

    async def on_language(room, language, event):
        language_events.append(event)

    async def on_complete(room, language, source, text):
        completed.append((room, language, source, text))
        return {"id": "caption-1", "text": text}

    hub = RealtimeTranslationHub(
        ignore_room,
        on_language,
        on_complete,
        api_key="test-key",
    )
    hub.sessions[("room", "speaker", "en-US")] = object()

    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {"type": "session.output_transcript.delta", "delta": "Hello "},
    )
    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {"type": "session.output_transcript.delta", "delta": "world"},
    )
    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {"type": "session.output_transcript.done"},
    )

    assert completed == [("room", "en-US", "speaker", "Hello world")]
    assert language_events[-1] == {
        "type": "translation_done",
        "source_id": "speaker",
        "caption": {"id": "caption-1", "text": "Hello world"},
    }
