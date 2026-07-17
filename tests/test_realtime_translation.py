import base64
import json

import pytest

from app.services.realtime_translation import (
    LANGUAGE_CODES,
    OpenAITranslationSession,
    RealtimeTranslationHub,
)


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
    await hub._route_event(
        "room",
        "speaker-a",
        "en-US",
        {"type": "session.input_transcript.done", "transcript": "안녕"},
    )

    assert room_events == [
        (
            "room",
            {
                "type": "transcript_delta",
                "text": "안녕",
                "source_id": "speaker-a",
            },
        ),
        (
            "room",
            {
                "type": "transcript_done",
                "source_id": "speaker-a",
                "text": "안녕",
            },
        ),
    ]


def test_supported_ui_languages_map_to_openai_language_codes():
    for language in (
        "ko-KR",
        "en-US",
        "zh-CN",
        "ja-JP",
        "es-ES",
        "pt-BR",
        "fr-FR",
        "de-DE",
        "ru-RU",
        "hi-IN",
        "id-ID",
        "it-IT",
        "vi-VN",
    ):
        assert language in LANGUAGE_CODES

    assert len(set(LANGUAGE_CODES.values())) == 13


@pytest.mark.asyncio
async def test_listener_count_never_multiplies_openai_routes(monkeypatch):
    created = []

    class FakeSession:
        def __init__(self, target_language, on_event, **kwargs):
            self.target_language = target_language
            created.append(self)

        def start(self):
            pass

        async def wait_until_ready(self):
            return True

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
async def test_new_audience_language_is_prepared_before_next_audio(monkeypatch):
    created = []

    class FakeSession:
        def __init__(self, target_language, on_event, **kwargs):
            self.target_language = target_language
            created.append(self)

        def start(self):
            pass

        async def wait_until_ready(self):
            return True

        async def close(self):
            pass

    monkeypatch.setattr(
        "app.services.realtime_translation.OpenAITranslationSession", FakeSession
    )

    async def ignore(*args):
        pass

    hub = RealtimeTranslationHub(ignore, ignore, api_key="test-key")
    await hub.prepare("room", "speaker", ["ko-KR"])
    await hub.prepare_language("room", "en-US")
    await hub.prepare_language("room", "en-US")

    assert [session.target_language for session in created] == ["ko", "en"]
    assert hub.active_session_count == 2


@pytest.mark.asyncio
async def test_translation_session_uses_low_latency_input_configuration(monkeypatch):
    connection_options = {}

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(json.loads(message))

        def __aiter__(self):
            async def events():
                yield json.dumps({"type": "session.closed"})

            return events()

    socket = FakeSocket()

    class FakeConnection:
        async def __aenter__(self):
            return socket

        async def __aexit__(self, *args):
            return False

    def fake_connect(uri, **kwargs):
        connection_options.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(
        "app.services.realtime_translation.websockets.connect", fake_connect
    )

    async def ignore(event):
        pass

    session = OpenAITranslationSession(
        "en",
        ignore,
        api_key="test-key",
        model="gpt-realtime-translate",
        safety_identifier="hashed-speaker-id",
    )
    await session._run_connection()

    update = socket.sent[0]
    input_audio = update["session"]["audio"]["input"]
    assert input_audio["transcription"]["model"] == "gpt-realtime-whisper"
    assert input_audio["noise_reduction"]["type"] == "near_field"
    assert update["session"]["audio"]["output"]["language"] == "en"
    assert connection_options["compression"] is None
    assert connection_options["additional_headers"]["OpenAI-Safety-Identifier"] == (
        "hashed-speaker-id"
    )


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


@pytest.mark.asyncio
async def test_continuous_translation_is_split_into_one_caption_per_sentence():
    language_events = []
    completed = []

    async def ignore_room(*args):
        pass

    async def on_language(room, language, event):
        language_events.append(event)

    async def on_complete(room, language, source, text):
        completed.append(text)
        return {"id": f"caption-{len(completed)}", "text": text}

    hub = RealtimeTranslationHub(
        ignore_room, on_language, on_complete, api_key="test-key"
    )
    hub.sessions[("room", "speaker", "en-US")] = object()
    continuous_text = (
        "So, hello. Hello. Today, I want us to read God's Word together. "
        "God loves you. Today, God has a happy message for us and wants "
        "to give it to us."
    )

    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {"type": "session.output_transcript.delta", "delta": continuous_text},
    )

    # All punctuated sentences, including the final one, are complete without
    # waiting for the provider's response-done event.
    assert completed == [
        "So, hello.",
        "Hello.",
        "Today, I want us to read God's Word together.",
        "God loves you.",
        "Today, God has a happy message for us and wants to give it to us.",
    ]

    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {"type": "session.output_transcript.done"},
    )

    assert completed == [
        "So, hello.",
        "Hello.",
        "Today, I want us to read God's Word together.",
        "God loves you.",
        "Today, God has a happy message for us and wants to give it to us.",
    ]
    reconstructed_current = ""
    reconstructed_history = []
    for event in language_events:
        if event["type"] == "translation_delta":
            reconstructed_current += event["text"]
        elif event["type"] == "translation_done":
            reconstructed_history.append(event["caption"]["text"])
            reconstructed_current = ""
    assert reconstructed_history == completed
    assert reconstructed_current == ""


@pytest.mark.asyncio
async def test_translation_done_completes_unpunctuated_caption():
    completed = []

    async def ignore(*args):
        pass

    async def on_complete(room, language, source, text):
        completed.append(text)
        return {"id": "caption-pause", "text": text}

    hub = RealtimeTranslationHub(
        ignore, ignore, on_complete, api_key="test-key"
    )
    hub.sessions[("room", "speaker", "en-US")] = object()

    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {
            "type": "session.output_transcript.delta",
            "delta": "A spoken phrase without punctuation",
        },
    )
    assert completed == []

    await hub._route_event(
        "room",
        "speaker",
        "en-US",
        {"type": "session.output_transcript.done"},
    )

    assert completed == ["A spoken phrase without punctuation"]
