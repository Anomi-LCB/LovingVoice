import asyncio
import base64
import binascii
import json
import logging
import os
import struct
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import WebSocket

logger = logging.getLogger("LovingVoice.Connection")

class ConnectionManager:
    def __init__(self):
        # 구조: {room_id: {lang: [websocket, websocket]}}
        self.active_connections = {}
        # New listeners can opt into a compact binary PCM stream. Keeping the
        # transport per websocket lets already-open/older pages continue to
        # receive the JSON/Base64 format during rolling deploys.
        self.audience_audio_transports = {}
        # Raw source audio is opt-in. It is used only as a quiet, immediate
        # listening aid until translated speech arrives, and never creates an
        # additional OpenAI translation route.
        self.source_audio_audiences = set()
        # 강연자 관리: {room_id: [websocket]}
        self.active_speakers = {}
        # Late joiners receive only a small, recent context window. Count, age,
        # item length and serialized payload are all bounded independently.
        self.subtitle_history = {}
        # A live room keeps its original internal channel when its public name
        # changes. New public names resolve to that same channel.
        self.room_aliases = {}
        self.history_limit = max(1, int(os.getenv("SUBTITLE_HISTORY_LIMIT", "20")))
        self.history_max_age_seconds = max(
            30, int(os.getenv("SUBTITLE_HISTORY_MAX_AGE_SECONDS", "300"))
        )
        self.history_max_bytes = max(
            1024, int(os.getenv("SUBTITLE_HISTORY_MAX_BYTES", "32768"))
        )
        self.history_item_max_chars = max(
            200, int(os.getenv("SUBTITLE_HISTORY_ITEM_MAX_CHARS", "2000"))
        )
        self.realtime_fanout_queue_size = max(
            4, min(64, int(os.getenv("REALTIME_FANOUT_QUEUE_SIZE", "8")))
        )
        self.realtime_send_timeout = max(
            0.1, min(2.0, float(os.getenv("REALTIME_SEND_TIMEOUT_SECONDS", "0.35")))
        )
        self._room_event_queues = {}
        self._room_event_workers = {}
        self._language_event_queues = {}
        self._language_event_workers = {}
        self._source_audio_queues = {}
        self._source_audio_workers = {}

    async def add_speaker(self, room_id, websocket):
        """방에 강연자를 추가한다. Realtime 모드는 인원 제한을 두지 않는다."""
        room_id = self.resolve_room_id(room_id)
        if room_id not in self.active_speakers:
            self.active_speakers[room_id] = []

        self.active_speakers[room_id].append(websocket)
        return True

    def remove_speaker(self, room_id, websocket):
        """강연자 세션 제거"""
        room_id = self.resolve_room_id(room_id)
        if room_id in self.active_speakers:
            try:
                self.active_speakers[room_id].remove(websocket)
                if not self.active_speakers[room_id]:
                    del self.active_speakers[room_id]
                    if room_id not in self.active_connections:
                        self._cancel_room_fanout(room_id)
            except ValueError:
                pass

    async def add_audience(
        self,
        room_id,
        lang,
        websocket,
        *,
        accept=True,
        audio_transport="json",
    ):
        room_id = self.resolve_room_id(room_id)
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        if lang not in self.active_connections[room_id]:
            self.active_connections[room_id][lang] = []
        self.active_connections[room_id][lang].append(websocket)
        self.audience_audio_transports[id(websocket)] = audio_transport
        if accept:
            await websocket.accept()

    def remove_audience(
        self, room_id, lang, websocket, *, preserve_transport=False
    ):
        room_id = self.resolve_room_id(room_id)
        if not preserve_transport:
            self.audience_audio_transports.pop(id(websocket), None)
            self.source_audio_audiences.discard(id(websocket))
        try:
            if room_id in self.active_connections and lang in self.active_connections[room_id]:
                self.active_connections[room_id][lang].remove(websocket)
                if not self.active_connections[room_id][lang]:
                    del self.active_connections[room_id][lang]
                    self._cancel_language_fanout(room_id, lang)
                if not self.active_connections[room_id]:
                    del self.active_connections[room_id]
                    self._cancel_source_audio_fanout(room_id)
                    if room_id not in self.active_speakers:
                        self._cancel_room_fanout(room_id)
        except ValueError:
            pass

    def set_source_audio_enabled(self, websocket, enabled):
        if enabled:
            self.source_audio_audiences.add(id(websocket))
        else:
            self.source_audio_audiences.discard(id(websocket))

    def has_source_audio_audience(self, room_id):
        room_id = self.resolve_room_id(room_id)
        return any(
            id(ws) in self.source_audio_audiences
            for websockets in self.active_connections.get(room_id, {}).values()
            for ws in websockets
        )

    async def broadcast_to_room(self, room_id, text, engine):
        """방에 있는 모든 언어별 청중에게 번역된 오디오를 병렬로 전송"""
        import logging
        logger = logging.getLogger("LovingVoice")
        
        room_id = self.resolve_room_id(room_id)
        room = self.active_connections.get(room_id, {})
        if not room:
            logger.error(f"[브로드캐스트 실패] {room_id}번 방에 연결된 청중이 없습니다.")
            return

        voice_mode = getattr(self, 'voice_modes', {}).get(room_id, 'speed')
        total_audience = sum(len(conns) for conns in room.values())
        logger.info(f"[브로드캐스트] {room_id}번 방: {total_audience}명 (voice={voice_mode})")

        async def _process_and_send(target_lang, websockets):
            try:
                # ── Phase 1: 번역 즉시 전송 (자막 먼저!) ──
                translated = await engine.translate_text(text, target_lang)
                logger.info(f"  [Phase1] {target_lang} 자막 즉시 전송!")

                text_msg = {"type": "translation", "text": translated, "audio": ""}
                active_ws = [ws for ws in websockets if ws.client_state.name == "CONNECTED"]
                if active_ws:
                    await asyncio.gather(*[ws.send_json(text_msg) for ws in active_ws])

                # ── Phase 2: TTS (voice_mode에 따라 분기) ──
                if voice_mode == "speed":
                    audio_b64 = await engine.generate_audio_fast(translated, target_lang)
                else:
                    audio_b64 = await engine.generate_audio(translated)
                
                if audio_b64:
                    audio_msg = {"type": "audio", "audio": audio_b64}
                    active_ws = [ws for ws in websockets if ws.client_state.name == "CONNECTED"]
                    if active_ws:
                        await asyncio.gather(*[ws.send_json(audio_msg) for ws in active_ws])
                    logger.info(f"  [Phase2] {target_lang} 음성 전송 ({voice_mode})")
            except Exception as e:
                logger.error(f"  [중단속행] {target_lang} 처리 중 예외 발생: {e}")

        # 모든 언어에 대해 독립적으로 병렬 처리 시작
        tasks = [
            _process_and_send(lang, conns) 
            for lang, conns in room.items() 
            if conns
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def broadcast_json_to_room(self, room_id, data):
        """Send a room-wide event to audiences and the room's speakers."""
        room_id = self.resolve_room_id(room_id)
        room = self.active_connections.get(room_id, {})
        send_tasks = []
        seen_websockets = set()
        for websockets in room.values():
            for ws in websockets:
                if ws.client_state.name == "CONNECTED" and id(ws) not in seen_websockets:
                    send_tasks.append(
                        asyncio.wait_for(
                            ws.send_json(data), timeout=self.realtime_send_timeout
                        )
                    )
                    seen_websockets.add(id(ws))
        for ws in self.active_speakers.get(room_id, []):
            if ws.client_state.name == "CONNECTED" and id(ws) not in seen_websockets:
                send_tasks.append(
                    asyncio.wait_for(
                        ws.send_json(data), timeout=self.realtime_send_timeout
                    )
                )
                seen_websockets.add(id(ws))
        
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    async def broadcast_json_to_language(self, room_id, lang, data):
        """Send a realtime event only to listeners of one output language."""
        room_id = self.resolve_room_id(room_id)
        room = self.active_connections.get(room_id, {})
        websockets = room.get(lang, [])
        connected = [ws for ws in websockets if ws.client_state.name == "CONNECTED"]
        binary_audio_frame = None
        if data.get("type") == "audio_delta" and any(
            self.audience_audio_transports.get(id(ws)) == "pcm16-v1"
            for ws in connected
        ):
            try:
                pcm = base64.b64decode(data.get("audio", ""), validate=True)
                source_id = (
                    str(data.get("source_id", "default"))
                    .encode("ascii", errors="ignore")[:32]
                    .ljust(32, b"\0")
                )
                # LV01 + uint32 little-endian sample rate + fixed 32-byte source id.
                # The 40-byte aligned header lets browsers create an Int16Array
                # directly over the websocket ArrayBuffer without another copy.
                binary_audio_frame = (
                    b"LV01"
                    + struct.pack("<I", int(data.get("sample_rate", 24000)))
                    + source_id
                    + pcm
                )
            except (TypeError, ValueError, binascii.Error):
                logger.warning("Invalid realtime PCM payload for %s/%s", room_id, lang)

        send_tasks = []
        for ws in connected:
            if (
                binary_audio_frame is not None
                and self.audience_audio_transports.get(id(ws)) == "pcm16-v1"
            ):
                send = ws.send_bytes(binary_audio_frame)
            else:
                send = ws.send_json(data)
            send_tasks.append(
                asyncio.wait_for(send, timeout=self.realtime_send_timeout)
            )
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    @staticmethod
    def _put_latest(queue, data):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(dict(data))

    async def queue_json_to_room(self, room_id, data):
        """Queue realtime room events so listener fan-out never blocks OpenAI."""
        room_id = self.resolve_room_id(room_id)
        queue = self._room_event_queues.get(room_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.realtime_fanout_queue_size)
            self._room_event_queues[room_id] = queue
            self._room_event_workers[room_id] = asyncio.create_task(
                self._room_fanout_worker(room_id, queue)
            )
        self._put_latest(queue, data)

    async def queue_json_to_language(self, room_id, lang, data):
        """Queue one shared realtime stream per room/language route."""
        room_id = self.resolve_room_id(room_id)
        key = (room_id, lang)
        queue = self._language_event_queues.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.realtime_fanout_queue_size)
            self._language_event_queues[key] = queue
            self._language_event_workers[key] = asyncio.create_task(
                self._language_fanout_worker(room_id, lang, queue)
            )
        self._put_latest(queue, data)

    async def queue_source_audio_to_room(
        self, room_id, pcm16, source_id, sample_rate=24000
    ):
        """Queue a bounded raw-source preview stream for opted-in listeners."""
        room_id = self.resolve_room_id(room_id)
        if not pcm16 or not self.has_source_audio_audience(room_id):
            return
        queue = self._source_audio_queues.get(room_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=4)
            self._source_audio_queues[room_id] = queue
            self._source_audio_workers[room_id] = asyncio.create_task(
                self._source_audio_fanout_worker(room_id, queue)
            )
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait((bytes(pcm16), str(source_id), int(sample_rate)))

    async def _room_fanout_worker(self, room_id, queue):
        try:
            while True:
                await self.broadcast_json_to_room(room_id, await queue.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Realtime room fan-out failed for %s", room_id)

    async def _language_fanout_worker(self, room_id, lang, queue):
        try:
            while True:
                await self.broadcast_json_to_language(
                    room_id, lang, await queue.get()
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Realtime language fan-out failed for %s/%s", room_id, lang
            )

    async def _source_audio_fanout_worker(self, room_id, queue):
        try:
            while True:
                pcm16, source_id, sample_rate = await queue.get()
                await self._broadcast_source_audio(
                    room_id, pcm16, source_id, sample_rate
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Source-audio fan-out failed for %s", room_id)

    async def _broadcast_source_audio(
        self, room_id, pcm16, source_id, sample_rate
    ):
        room_id = self.resolve_room_id(room_id)
        source_bytes = (
            str(source_id)
            .encode("ascii", errors="ignore")[:32]
            .ljust(32, b"\0")
        )
        frame = b"LVS1" + struct.pack("<I", sample_rate) + source_bytes + pcm16
        targets = [
            ws
            for websockets in self.active_connections.get(room_id, {}).values()
            for ws in websockets
            if ws.client_state.name == "CONNECTED"
            and id(ws) in self.source_audio_audiences
            and self.audience_audio_transports.get(id(ws)) == "pcm16-v1"
        ]
        if targets:
            await asyncio.gather(
                *(
                    asyncio.wait_for(
                        ws.send_bytes(frame), timeout=self.realtime_send_timeout
                    )
                    for ws in targets
                ),
                return_exceptions=True,
            )

    def _cancel_room_fanout(self, room_id):
        worker = self._room_event_workers.pop(room_id, None)
        self._room_event_queues.pop(room_id, None)
        if worker:
            worker.cancel()

    def _cancel_source_audio_fanout(self, room_id):
        worker = self._source_audio_workers.pop(room_id, None)
        self._source_audio_queues.pop(room_id, None)
        if worker:
            worker.cancel()

    def _cancel_language_fanout(self, room_id, lang):
        key = (room_id, lang)
        worker = self._language_event_workers.pop(key, None)
        self._language_event_queues.pop(key, None)
        if worker:
            worker.cancel()

    async def close_realtime_fanout(self):
        workers = [
            *self._room_event_workers.values(),
            *self._language_event_workers.values(),
            *self._source_audio_workers.values(),
        ]
        self._room_event_workers.clear()
        self._language_event_workers.clear()
        self._source_audio_workers.clear()
        self._room_event_queues.clear()
        self._language_event_queues.clear()
        self._source_audio_queues.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def record_subtitle(self, room_id, lang, source_id, text):
        """Store a completed translation and return its public event payload."""
        room_id = self.resolve_room_id(room_id)
        text = text.strip()
        if not text:
            return None
        history_text = text[: self.history_item_max_chars]
        item = {
            "id": uuid.uuid4().hex,
            "text": history_text,
            "source_id": source_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        room_history = self.subtitle_history.setdefault(room_id, {})
        language_history = room_history.setdefault(lang, [])
        language_history.append(item)
        self._prune_subtitle_history(room_id, lang)
        return item

    def get_subtitle_history(self, room_id, lang):
        """Return newest context within the configured count, age and byte budget."""
        room_id = self.resolve_room_id(room_id)
        self._prune_subtitle_history(room_id, lang)
        items = self.subtitle_history.get(room_id, {}).get(lang, [])
        selected = []
        serialized_bytes = 2  # JSON array brackets
        for item in reversed(items):
            item_size = len(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ) + 1
            if selected and serialized_bytes + item_size > self.history_max_bytes:
                break
            if item_size > self.history_max_bytes:
                continue
            selected.append(dict(item))
            serialized_bytes += item_size
        selected.reverse()
        return selected

    def _prune_subtitle_history(self, room_id, lang):
        room_id = self.resolve_room_id(room_id)
        language_history = self.subtitle_history.get(room_id, {}).get(lang, [])
        if not language_history:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.history_max_age_seconds
        )
        recent_items = []
        for item in language_history:
            try:
                created_at = datetime.fromisoformat(item["created_at"])
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if created_at >= cutoff:
                recent_items.append(item)
        language_history[:] = recent_items[-self.history_limit :]

    def audience_languages(self, room_id):
        """Return distinct language routes, never one entry per listener."""
        room_id = self.resolve_room_id(room_id)
        room = self.active_connections.get(room_id, {})
        return [lang for lang, sockets in room.items() if sockets]

    def realtime_languages(self, room_id, speaker_fallback="ko-KR"):
        """Keep one source-caption route alive when a speaker has no audience yet."""
        languages = self.audience_languages(room_id)
        return languages or [speaker_fallback]

    def room_status(self, room_id):
        """Return public room fan-out statistics without exposing connections."""
        public_room_id = room_id
        room_id = self.resolve_room_id(room_id)
        room = self.active_connections.get(room_id, {})
        audience_by_language = {
            lang: len(sockets) for lang, sockets in room.items() if sockets
        }
        history_counts = {}
        for lang in list(self.subtitle_history.get(room_id, {})):
            count = len(self.get_subtitle_history(room_id, lang))
            if count:
                history_counts[lang] = count
        return {
            "room_id": public_room_id,
            "exists": room_id in self.active_speakers,
            "speaker_count": len(self.active_speakers.get(room_id, [])),
            "audience_count": sum(audience_by_language.values()),
            "audience_by_language": audience_by_language,
            "shared_language_channels": list(audience_by_language),
            "history_available_by_language": history_counts,
            "recent_history_policy": {
                "max_items": self.history_limit,
                "max_age_seconds": self.history_max_age_seconds,
                "max_bytes": self.history_max_bytes,
            },
        }

    def resolve_room_id(self, room_id):
        """Return the stable internal channel for a public room name."""
        return self.room_aliases.get(room_id, room_id)

    def register_room_alias(self, current_room_id, new_room_id):
        """Route a renamed public room to its existing live channel."""
        internal_room_id = self.resolve_room_id(current_room_id)
        self.room_aliases[new_room_id] = internal_room_id
        return internal_room_id
