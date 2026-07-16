"""OpenAI Realtime Translate sessions for LovingVoice.

The browser sends 24 kHz PCM16 microphone chunks to the FastAPI server.  This
module maintains one OpenAI translation WebSocket per source/output language
and fans the translated PCM and transcript events back to every listener of
that language. Audience count never changes the number of OpenAI sessions.
The standard API key never leaves the server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Iterable

import websockets

logger = logging.getLogger("LovingVoice.Realtime")

RoomCallback = Callable[[str, dict], Awaitable[None]]
LanguageCallback = Callable[[str, str, dict], Awaitable[None]]
CompletionCallback = Callable[
    [str, str, str, str], Awaitable[dict | None]
]


LANGUAGE_CODES = {
    "ko-KR": "ko",
    "en-US": "en",
    "zh-CN": "zh",
    "ja-JP": "ja",
    "fr-FR": "fr",
    "de-DE": "de",
    "es-ES": "es",
    "pt-BR": "pt",
    "ru-RU": "ru",
    "hi-IN": "hi",
    "id-ID": "id",
    "it-IT": "it",
    "vi-VN": "vi",
}


class OpenAITranslationSession:
    """A resilient WebSocket session for one target language."""

    def __init__(
        self,
        target_language: str,
        on_event: Callable[[dict], Awaitable[None]],
        *,
        api_key: str,
        model: str = "gpt-realtime-translate",
    ) -> None:
        self.target_language = target_language
        self.on_event = on_event
        self.api_key = api_key
        self.model = model
        # Keep only a short window of 20 ms input frames. Live interpretation is
        # more useful with a tiny discontinuity than audio replayed seconds late.
        backlog_ms = max(
            100, min(2000, int(os.getenv("OPENAI_REALTIME_AUDIO_BACKLOG_MS", "400")))
        )
        self.audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=math.ceil(backlog_ms / 20)
        )
        self.task: asyncio.Task | None = None
        self.closed = False
        self.dropped_frames = 0
        self.first_audio_sent_at: float | None = None
        self.first_output_logged = False

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def feed(self, pcm16: bytes) -> None:
        if self.closed:
            return
        self.start()
        if self.audio_queue.full():
            try:
                self.audio_queue.get_nowait()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
        self.audio_queue.put_nowait(pcm16)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.audio_queue.put_nowait(None)
        except asyncio.QueueFull:
            await self.audio_queue.put(None)
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()

    async def _run(self) -> None:
        retry_delay = 0.5
        while not self.closed:
            try:
                await self._run_connection()
                retry_delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Realtime translation connection failed (%s)", self.target_language)
                await self.on_event(
                    {
                        "type": "engine_error",
                        "message": f"OpenAI 실시간 통역 연결 오류: {exc}",
                    }
                )
                if self.closed:
                    break
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 8)

    async def _run_connection(self) -> None:
        uri = (
            "wss://api.openai.com/v1/realtime/translations"
            f"?model={self.model}"
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Safety-Identifier": "lovingvoice-server",
        }
        async with websockets.connect(
            uri,
            additional_headers=headers,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            compression=None,
            max_queue=32,
            write_limit=32 * 1024,
            max_size=8 * 1024 * 1024,
        ) as socket:
            self.first_audio_sent_at = None
            self.first_output_logged = False
            await socket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "audio": {
                                "input": {
                                    "transcription": {
                                        "model": "gpt-realtime-whisper"
                                    },
                                    "noise_reduction": {"type": "near_field"},
                                },
                                "output": {"language": self.target_language},
                            }
                        },
                    }
                )
            )
            await self.on_event(
                {
                    "type": "engine_status",
                    "state": "online",
                    "message": f"GPT Realtime 통역 연결됨 ({self.target_language})",
                }
            )

            sender = asyncio.create_task(self._send_audio(socket))
            receiver = asyncio.create_task(self._receive_events(socket))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            if sender in done and self.closed and not receiver.done():
                # Translation sessions flush their final transcript/audio after
                # session.close. Keep receiving until session.closed arrives.
                try:
                    await asyncio.wait_for(receiver, timeout=4)
                except asyncio.TimeoutError:
                    receiver.cancel()
                return
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
            if not self.closed:
                raise ConnectionError("OpenAI Realtime socket closed unexpectedly")

    async def _send_audio(self, socket) -> None:
        while True:
            chunk = await self.audio_queue.get()
            if chunk is None:
                try:
                    await socket.send(json.dumps({"type": "session.close"}))
                except websockets.ConnectionClosed:
                    pass
                return
            if self.first_audio_sent_at is None:
                self.first_audio_sent_at = time.monotonic()
                self.first_output_logged = False
            await socket.send(
                json.dumps(
                    {
                        "type": "session.input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                )
            )

    async def _receive_events(self, socket) -> None:
        async for raw_event in socket:
            event = json.loads(raw_event)
            event_type = event.get("type", "")
            if event_type == "error":
                error = event.get("error", {})
                raise RuntimeError(error.get("message", "Unknown OpenAI Realtime error"))
            if (
                event_type == "session.output_audio.delta"
                and self.first_audio_sent_at is not None
                and not self.first_output_logged
            ):
                latency_ms = round((time.monotonic() - self.first_audio_sent_at) * 1000)
                logger.info(
                    "Realtime first translated audio (%s): %d ms",
                    self.target_language,
                    latency_ms,
                )
                self.first_output_logged = True
            await self.on_event(event)
            if event_type == "session.closed":
                self.closed = True
                return


class RealtimeTranslationHub:
    """Own OpenAI sessions and route their output to LovingVoice audiences."""

    def __init__(
        self,
        on_room_event: RoomCallback,
        on_language_event: LanguageCallback,
        on_translation_complete: CompletionCallback | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv(
            "OPENAI_REALTIME_MODEL", "gpt-realtime-translate"
        )
        self.on_room_event = on_room_event
        self.on_language_event = on_language_event
        self.on_translation_complete = on_translation_complete
        # A separate translation session is required for every source speaker
        # track and target language. This avoids mixing simultaneous speakers.
        self.sessions: dict[
            tuple[str, str, str], OpenAITranslationSession
        ] = {}
        self.transcript_buffers: dict[tuple[str, str, str], str] = {}
        self.source_transcript_buffers: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def active_session_count(self) -> int:
        return len(self.sessions)

    def route_status(self, room_id: str) -> list[dict[str, str]]:
        """Return unique billable translation routes for room observability."""
        return [
            {"source_id": source, "language": language}
            for room, source, language in self.sessions
            if room == room_id
        ]

    async def feed(
        self,
        room_id: str,
        source_id: str,
        pcm16: bytes,
        languages: Iterable[str],
    ) -> None:
        if not self.available:
            raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
        sessions = await self._sync_languages(room_id, source_id, languages)
        if sessions:
            await asyncio.gather(*(session.feed(pcm16) for session in sessions))

    async def prepare(
        self, room_id: str, source_id: str, languages: Iterable[str]
    ) -> None:
        """Open translation routes before the first microphone frame arrives."""
        if not self.available:
            return
        await self._sync_languages(room_id, source_id, languages)

    async def prepare_language(self, room_id: str, language: str) -> None:
        """Warm a new audience language for every active source in the room."""
        if not self.available or language not in LANGUAGE_CODES:
            return
        async with self._lock:
            source_languages: dict[str, set[str]] = {}
            for room, source, current_language in self.sessions:
                if room == room_id:
                    source_languages.setdefault(source, set()).add(current_language)
        if source_languages:
            await asyncio.gather(
                *(
                    self._sync_languages(
                        room_id, source, current_languages | {language}
                    )
                    for source, current_languages in source_languages.items()
                )
            )

    async def _sync_languages(
        self, room_id: str, source_id: str, languages: Iterable[str]
    ) -> list[OpenAITranslationSession]:
        wanted = {language for language in languages if language in LANGUAGE_CODES}
        async with self._lock:
            current_languages = {
                language
                for room, source, language in self.sessions
                if room == room_id and source == source_id
            }
            for language in wanted - current_languages:
                target = LANGUAGE_CODES[language]

                async def on_event(event: dict, lang: str = language) -> None:
                    await self._route_event(room_id, source_id, lang, event)

                session = OpenAITranslationSession(
                    target,
                    on_event,
                    api_key=self.api_key,
                    model=self.model,
                )
                self.sessions[(room_id, source_id, language)] = session
                session.start()

            stale_keys = {
                key
                for key in self.sessions
                if key[0] == room_id and key[1] == source_id and key[2] not in wanted
            }
            stale_sessions = [self.sessions.pop(key) for key in stale_keys]
            active = [
                session
                for (room, source, _), session in self.sessions.items()
                if room == room_id and source == source_id
            ]

        if stale_sessions:
            await asyncio.gather(*(session.close() for session in stale_sessions))
        return active

    async def _route_event(
        self, room_id: str, source_id: str, language: str, event: dict
    ) -> None:
        event_type = event.get("type", "")
        if event_type == "session.output_audio.delta":
            await self.on_language_event(
                room_id,
                language,
                {
                    "type": "audio_delta",
                    "audio": event.get("delta", ""),
                    "format": "pcm16",
                    "sample_rate": 24000,
                    "source_id": source_id,
                },
            )
        elif event_type == "session.output_transcript.delta":
            key = (room_id, source_id, language)
            self.transcript_buffers[key] = self.transcript_buffers.get(key, "") + event.get(
                "delta", ""
            )
            await self.on_language_event(
                room_id,
                language,
                {
                    "type": "translation_delta",
                    "text": event.get("delta", ""),
                    "source_id": source_id,
                },
            )
        elif event_type == "session.output_transcript.done":
            key = (room_id, source_id, language)
            completed_text = self.transcript_buffers.pop(key, "").strip()
            if not completed_text:
                completed_text = str(event.get("transcript", "")).strip()
            caption = None
            if completed_text and self.on_translation_complete:
                caption = await self.on_translation_complete(
                    room_id, language, source_id, completed_text
                )
            await self.on_language_event(
                room_id,
                language,
                {
                    "type": "translation_done",
                    "source_id": source_id,
                    "caption": caption,
                },
            )
        elif event_type == "session.input_transcript.delta":
            # Publish each source transcript once even when it has many targets.
            primary = next(
                (
                    lang
                    for room, source, lang in self.sessions
                    if room == room_id and source == source_id
                ),
                None,
            )
            if language == primary:
                source_key = (room_id, source_id)
                self.source_transcript_buffers[source_key] = (
                    self.source_transcript_buffers.get(source_key, "")
                    + event.get("delta", "")
                )
                await self.on_room_event(
                    room_id,
                    {
                        "type": "transcript_delta",
                        "text": event.get("delta", ""),
                        "source_id": source_id,
                    },
                )
        elif event_type == "session.input_transcript.done":
            primary = next(
                (
                    lang
                    for room, source, lang in self.sessions
                    if room == room_id and source == source_id
                ),
                None,
            )
            if language == primary:
                source_key = (room_id, source_id)
                completed_text = self.source_transcript_buffers.pop(
                    source_key, ""
                ).strip()
                if not completed_text:
                    completed_text = str(event.get("transcript", "")).strip()
                await self.on_room_event(
                    room_id,
                    {
                        "type": "transcript_done",
                        "source_id": source_id,
                        "text": completed_text,
                    },
                )
        elif event_type == "engine_status":
            await self.on_language_event(
                room_id, language, {**event, "source_id": source_id}
            )
        elif event_type == "engine_error":
            await self.on_language_event(
                room_id, language, {**event, "source_id": source_id}
            )

    async def close_speaker(self, room_id: str, source_id: str) -> None:
        async with self._lock:
            keys = [
                key
                for key in self.sessions
                if key[0] == room_id and key[1] == source_id
            ]
            speaker_sessions = [self.sessions.pop(key) for key in keys]
            for key in keys:
                self.transcript_buffers.pop(key, None)
            self.source_transcript_buffers.pop((room_id, source_id), None)
        if speaker_sessions:
            await asyncio.gather(
                *(session.close() for session in speaker_sessions),
                return_exceptions=True,
            )

    async def close_room(self, room_id: str) -> None:
        async with self._lock:
            keys = [key for key in self.sessions if key[0] == room_id]
            room_sessions = [self.sessions.pop(key) for key in keys]
            for key in keys:
                self.transcript_buffers.pop(key, None)
            for source_key in [
                key for key in self.source_transcript_buffers if key[0] == room_id
            ]:
                self.source_transcript_buffers.pop(source_key, None)
        if room_sessions:
            await asyncio.gather(
                *(session.close() for session in room_sessions),
                return_exceptions=True,
            )

    async def close(self) -> None:
        rooms = list({room for room, _, _ in self.sessions})
        await asyncio.gather(*(self.close_room(room) for room in rooms))
