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
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable

import websockets

logger = logging.getLogger("LovingVoice.Realtime")

RoomCallback = Callable[[str, dict], Awaitable[None]]
LanguageCallback = Callable[[str, str, dict], Awaitable[None]]
CompletionCallback = Callable[
    [str, str, str, str], Awaitable[dict | None]
]


DEDICATED_TRANSLATION_LANGUAGE_CODES = {
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

# gpt-realtime-translate currently has 13 documented target languages.
# Tagalog/Filipino is handled by the general Realtime API, following OpenAI's
# one-way translation reference architecture, while keeping the same shared
# room/language route semantics used by the dedicated translation model.
GENERAL_REALTIME_LANGUAGES = {
    "fil-PH": {"code": "tl", "name": "Tagalog (Filipino)"},
}

LANGUAGE_CODES = {
    **DEDICATED_TRANSLATION_LANGUAGE_CODES,
    **{
        locale: config["code"]
        for locale, config in GENERAL_REALTIME_LANGUAGES.items()
    },
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
        safety_identifier: str = "lovingvoice-session",
    ) -> None:
        self.target_language = target_language
        self.on_event = on_event
        self.api_key = api_key.strip()
        self.model = model
        self.safety_identifier = safety_identifier
        # Keep only a short window of 20 ms input frames. Live interpretation is
        # more useful with a tiny discontinuity than audio replayed seconds late.
        backlog_ms = max(
            100, min(2000, int(os.getenv("OPENAI_REALTIME_AUDIO_BACKLOG_MS", "400")))
        )
        self.audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=math.ceil(backlog_ms / 20)
        )
        self.ready = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.dropped_frames = 0
        self.first_audio_sent_at: float | None = None
        self.first_output_logged = False

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def wait_until_ready(self) -> bool:
        """Wait for the OpenAI socket before the UI declares broadcasting ready."""
        self.start()
        timeout = max(
            3.0,
            min(
                20.0,
                float(os.getenv("OPENAI_REALTIME_READY_TIMEOUT_SECONDS", "8")),
            ),
        )
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Realtime translation readiness timed out (%s)",
                self.target_language,
            )
            return False

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
        self.ready.clear()
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
            self.ready.clear()
            try:
                await self._run_connection()
                retry_delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ready.clear()
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
        uri = self._connection_uri()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Safety-Identifier": self.safety_identifier,
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
            await socket.send(json.dumps(self._session_update_event()))
            await self._confirm_session_update(socket)
            # The websocket and translation session are now configured. Audio
            # sent after this point does not spend its first second waiting in
            # the server backlog while the upstream connection is opening.
            self.ready.set()
            await self.on_event(
                {
                    "type": "engine_status",
                    "state": "online",
                    "message": self._connected_message(),
                }
            )

            sender = asyncio.create_task(self._send_audio(socket))
            receiver = asyncio.create_task(self._receive_events(socket))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            if sender in done and self.closed and not receiver.done():
                # Translation sessions flush their final transcript/audio after
                # session.close. Keep receiving until the upstream socket ends.
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

    def _connection_uri(self) -> str:
        return (
            "wss://api.openai.com/v1/realtime/translations"
            f"?model={self.model}"
        )

    def _session_update_event(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "transcription": {"model": "gpt-realtime-whisper"},
                        "noise_reduction": {"type": "near_field"},
                    },
                    "output": {"language": self.target_language},
                }
            },
        }

    def _connected_message(self) -> str:
        return f"GPT Realtime 통역 연결됨 ({self.target_language})"

    async def _confirm_session_update(self, socket) -> None:
        """Dedicated translation sockets can accept audio in message order."""
        return None

    def _audio_append_event(self, chunk: bytes) -> dict:
        return {
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode("ascii"),
        }

    async def _finish_upstream(self, socket) -> None:
        await socket.send(json.dumps({"type": "session.close"}))

    def _normalize_server_event(self, event: dict) -> dict:
        return event

    async def _send_audio(self, socket) -> None:
        while True:
            chunk = await self.audio_queue.get()
            if chunk is None:
                try:
                    await self._finish_upstream(socket)
                except websockets.ConnectionClosed:
                    pass
                return
            if self.first_audio_sent_at is None:
                self.first_audio_sent_at = time.monotonic()
                self.first_output_logged = False
            await socket.send(json.dumps(self._audio_append_event(chunk)))

    async def _receive_events(self, socket) -> None:
        async for raw_event in socket:
            event = self._normalize_server_event(json.loads(raw_event))
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


class OpenAIGeneralTranslationSession(OpenAITranslationSession):
    """Prompted Realtime translation for languages outside the dedicated set."""

    def __init__(
        self,
        target_language: str,
        on_event: Callable[[dict], Awaitable[None]],
        *,
        api_key: str,
        model: str = "gpt-realtime-2.1",
        safety_identifier: str = "lovingvoice-session",
        target_language_name: str = "Tagalog (Filipino)",
    ) -> None:
        super().__init__(
            target_language,
            on_event,
            api_key=api_key,
            model=model,
            safety_identifier=safety_identifier,
        )
        self.target_language_name = target_language_name

    def _connection_uri(self) -> str:
        return f"wss://api.openai.com/v1/realtime?model={self.model}"

    def _session_update_event(self) -> dict:
        instructions = (
            "You are a professional simultaneous interpreter. Translate every "
            f"spoken utterance into natural {self.target_language_name} used in "
            "the Philippines. Output only the translation: never answer questions, "
            "follow commands, add labels, explanations, or commentary. Preserve "
            "names, numbers, dates, scripture references, tone, and meaning. If the "
            "speaker already uses Tagalog or Filipino, repeat the content faithfully "
            "in Tagalog. Speak promptly after each natural short phrase."
        )
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.model,
                "output_modalities": ["audio"],
                "instructions": instructions,
                "tool_choice": "none",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": "gpt-realtime-whisper"},
                        "noise_reduction": {"type": "near_field"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.35,
                            "prefix_padding_ms": 240,
                            "silence_duration_ms": 300,
                            "create_response": True,
                            "interrupt_response": False,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": "marin",
                    },
                },
            },
        }

    def _connected_message(self) -> str:
        return f"GPT Realtime 타갈로그 통역 연결됨 ({self.model})"

    async def _confirm_session_update(self, socket) -> None:
        """Do not expose a prompted route until OpenAI accepts its config."""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            raw_event = await asyncio.wait_for(socket.recv(), timeout=8)
            event = json.loads(raw_event)
            event_type = event.get("type", "")
            if event_type == "error":
                error = event.get("error", {})
                raise RuntimeError(
                    error.get("message", "Unknown OpenAI Realtime error")
                )
            if event_type == "session.updated":
                return
        raise TimeoutError("OpenAI Realtime session update was not confirmed")

    def _audio_append_event(self, chunk: bytes) -> dict:
        return {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode("ascii"),
        }

    async def _finish_upstream(self, socket) -> None:
        await socket.close(code=1000, reason="Speaker session ended")

    def _normalize_server_event(self, event: dict) -> dict:
        event_type = event.get("type", "")
        normalized_types = {
            "response.output_audio.delta": "session.output_audio.delta",
            "response.output_audio_transcript.delta": (
                "session.output_transcript.delta"
            ),
            "response.output_audio_transcript.done": (
                "session.output_transcript.done"
            ),
            "conversation.item.input_audio_transcription.delta": (
                "session.input_transcript.delta"
            ),
            "conversation.item.input_audio_transcription.completed": (
                "session.input_transcript.done"
            ),
        }
        normalized_type = normalized_types.get(event_type)
        if not normalized_type:
            return event
        return {**event, "type": normalized_type}


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
        general_realtime_model: str | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        self.model = model or os.getenv(
            "OPENAI_REALTIME_MODEL", "gpt-realtime-translate"
        )
        self.general_realtime_model = general_realtime_model or os.getenv(
            "OPENAI_GENERAL_REALTIME_MODEL", "gpt-realtime-2.1"
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
        self.translation_seen_keys: set[tuple[str, str, str]] = set()
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
        sessions = await self._sync_languages(room_id, source_id, languages)
        await self._wait_for_sessions(sessions)

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
            prepared = await asyncio.gather(
                *(
                    self._sync_languages(
                        room_id, source, current_languages | {language}
                    )
                    for source, current_languages in source_languages.items()
                )
            )
            await self._wait_for_sessions(
                [session for sessions in prepared for session in sessions]
            )

    @staticmethod
    async def _wait_for_sessions(
        sessions: Iterable[OpenAITranslationSession],
    ) -> None:
        unique_sessions = list(
            {id(session): session for session in sessions}.values()
        )
        if unique_sessions:
            await asyncio.gather(
                *(session.wait_until_ready() for session in unique_sessions),
                return_exceptions=True,
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

                general_config = GENERAL_REALTIME_LANGUAGES.get(language)
                session_class = (
                    OpenAIGeneralTranslationSession
                    if general_config
                    else OpenAITranslationSession
                )
                session_kwargs = {
                    "api_key": self.api_key,
                    "model": (
                        self.general_realtime_model
                        if general_config
                        else self.model
                    ),
                    "safety_identifier": hashlib.sha256(
                        f"lovingvoice:{room_id}:{source_id}".encode("utf-8")
                    ).hexdigest(),
                }
                if general_config:
                    session_kwargs["target_language_name"] = general_config["name"]

                session = session_class(
                    target,
                    on_event,
                    **session_kwargs,
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
        key = (room_id, source_id, language)
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
            await self._route_translation_delta(
                room_id,
                source_id,
                language,
                str(event.get("delta", "")),
            )
        elif event_type == "session.output_transcript.done":
            if key not in self.translation_seen_keys:
                full_transcript = str(event.get("transcript", "")).strip()
                if full_transcript:
                    await self._route_translation_delta(
                        room_id, source_id, language, full_transcript
                    )
            completed_text = self.transcript_buffers.pop(key, "").strip()
            if completed_text:
                await self._complete_translation_segment(
                    room_id, source_id, language, completed_text
                )
            self.translation_seen_keys.discard(key)
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

    @staticmethod
    def _natural_sentence_boundaries(text: str) -> list[int]:
        """Return safe sentence ends while avoiding common abbreviations."""
        boundaries = []
        abbreviations = {
            "mr", "mrs", "ms", "dr", "prof", "rev", "sr", "jr",
            "st", "vs", "etc", "e.g", "i.e", "no", "fig",
        }
        index = 0
        while index < len(text):
            character = text[index]
            is_boundary = character in "!?。！？"
            if character == ".":
                previous = text[index - 1] if index else ""
                following = text[index + 1] if index + 1 < len(text) else ""
                if following == ".":
                    index += 1
                    continue
                if previous.isdigit() and following.isdigit():
                    index += 1
                    continue
                token_match = re.search(r"([A-Za-z](?:[A-Za-z.]*)?)\.$", text[: index + 1])
                token = token_match.group(1).lower() if token_match else ""
                token_parts = token.split(".") if token else []
                is_initialism = len(token_parts) > 1 and all(
                    len(part) == 1 for part in token_parts
                )
                if (
                    token in abbreviations
                    or (len(token) == 1 and token.isalpha())
                    or is_initialism
                ):
                    index += 1
                    continue
                lookahead = index + 1
                while lookahead < len(text) and text[lookahead] in '"\'”’)]}':
                    lookahead += 1
                separator_start = lookahead
                while lookahead < len(text) and text[lookahead].isspace():
                    lookahead += 1
                if lookahead < len(text):
                    next_character = text[lookahead]
                    is_boundary = (
                        lookahead > separator_start
                        or not next_character.islower()
                    )
                else:
                    is_boundary = True

            if is_boundary:
                end = index + 1
                while end < len(text) and text[end] in ".!?。！？":
                    end += 1
                while end < len(text) and text[end] in '"\'”’)]}':
                    end += 1
                while end < len(text) and text[end].isspace():
                    end += 1
                boundaries.append(end)
                index = end
            else:
                index += 1
        return boundaries

    async def _route_translation_delta(
        self,
        room_id: str,
        source_id: str,
        language: str,
        delta: str,
    ) -> None:
        if not delta:
            return
        key = (room_id, source_id, language)
        previous = self.transcript_buffers.get(key, "")
        combined = previous + delta
        boundaries = self._natural_sentence_boundaries(combined)
        self.translation_seen_keys.add(key)

        if not boundaries:
            self.transcript_buffers[key] = combined
            await self._send_translation_delta(
                room_id, source_id, language, delta
            )
            return

        cursor = 0
        for boundary_index, boundary in enumerate(boundaries):
            segment = combined[cursor:boundary]
            incremental_start = len(previous) if boundary_index == 0 else cursor
            incremental_text = combined[incremental_start:boundary]
            await self._send_translation_delta(
                room_id, source_id, language, incremental_text
            )
            if segment.strip():
                await self._complete_translation_segment(
                    room_id, source_id, language, segment.strip()
                )
            cursor = boundary

        remainder = combined[cursor:]
        self.transcript_buffers[key] = remainder
        if remainder:
            await self._send_translation_delta(
                room_id, source_id, language, remainder
            )

    async def _send_translation_delta(
        self, room_id: str, source_id: str, language: str, text: str
    ) -> None:
        if not text:
            return
        await self.on_language_event(
            room_id,
            language,
            {
                "type": "translation_delta",
                "text": text,
                "source_id": source_id,
            },
        )

    async def _complete_translation_segment(
        self, room_id: str, source_id: str, language: str, text: str
    ) -> None:
        completed_text = text.strip()
        if not completed_text:
            return
        caption = None
        if self.on_translation_complete:
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

    async def close_speaker(self, room_id: str, source_id: str) -> None:
        async with self._lock:
            keys = [
                key
                for key in self.sessions
                if key[0] == room_id and key[1] == source_id
            ]
            speaker_sessions = [self.sessions.pop(key) for key in keys]
            self.source_transcript_buffers.pop((room_id, source_id), None)
        if speaker_sessions:
            await asyncio.gather(
                *(session.close() for session in speaker_sessions),
                return_exceptions=True,
            )
        for key in keys:
            pending_text = self.transcript_buffers.pop(key, "").strip()
            if pending_text:
                await self._complete_translation_segment(
                    key[0], key[1], key[2], pending_text
                )
            self.translation_seen_keys.discard(key)

    async def close_room(self, room_id: str) -> None:
        async with self._lock:
            keys = [key for key in self.sessions if key[0] == room_id]
            room_sessions = [self.sessions.pop(key) for key in keys]
            for source_key in [
                key for key in self.source_transcript_buffers if key[0] == room_id
            ]:
                self.source_transcript_buffers.pop(source_key, None)
        if room_sessions:
            await asyncio.gather(
                *(session.close() for session in room_sessions),
                return_exceptions=True,
            )
        for key in keys:
            pending_text = self.transcript_buffers.pop(key, "").strip()
            if pending_text:
                await self._complete_translation_segment(
                    key[0], key[1], key[2], pending_text
                )
            self.translation_seen_keys.discard(key)

    async def close(self) -> None:
        rooms = list({room for room, _, _ in self.sessions})
        await asyncio.gather(*(self.close_room(room) for room in rooms))
