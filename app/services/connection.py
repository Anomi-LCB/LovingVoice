import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # 구조: {room_id: {lang: [websocket, websocket]}}
        self.active_connections = {}
        # 강연자 관리: {room_id: [websocket]}
        self.active_speakers = {}
        # Late joiners receive only a small, recent context window. Count, age,
        # item length and serialized payload are all bounded independently.
        self.subtitle_history = {}
        self.history_limit = max(1, int(os.getenv("SUBTITLE_HISTORY_LIMIT", "30")))
        self.history_max_age_seconds = max(
            30, int(os.getenv("SUBTITLE_HISTORY_MAX_AGE_SECONDS", "300"))
        )
        self.history_max_bytes = max(
            1024, int(os.getenv("SUBTITLE_HISTORY_MAX_BYTES", "32768"))
        )
        self.history_item_max_chars = max(
            200, int(os.getenv("SUBTITLE_HISTORY_ITEM_MAX_CHARS", "2000"))
        )

    async def add_speaker(self, room_id, websocket):
        """방에 강연자를 추가한다. Realtime 모드는 인원 제한을 두지 않는다."""
        if room_id not in self.active_speakers:
            self.active_speakers[room_id] = []

        self.active_speakers[room_id].append(websocket)
        return True

    def remove_speaker(self, room_id, websocket):
        """강연자 세션 제거"""
        if room_id in self.active_speakers:
            try:
                self.active_speakers[room_id].remove(websocket)
                if not self.active_speakers[room_id]:
                    del self.active_speakers[room_id]
            except ValueError:
                pass

    async def add_audience(self, room_id, lang, websocket, *, accept=True):
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        if lang not in self.active_connections[room_id]:
            self.active_connections[room_id][lang] = []
        self.active_connections[room_id][lang].append(websocket)
        if accept:
            await websocket.accept()

    def remove_audience(self, room_id, lang, websocket):
        try:
            if room_id in self.active_connections and lang in self.active_connections[room_id]:
                self.active_connections[room_id][lang].remove(websocket)
                if not self.active_connections[room_id][lang]:
                    del self.active_connections[room_id][lang]
                if not self.active_connections[room_id]:
                    del self.active_connections[room_id]
        except ValueError:
            pass

    async def broadcast_to_room(self, room_id, text, engine):
        """방에 있는 모든 언어별 청중에게 번역된 오디오를 병렬로 전송"""
        import logging
        logger = logging.getLogger("LovingVoice")
        
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
        """방에 있는 모든 청중에게 JSON 메시지를 실시간 전송"""
        room = self.active_connections.get(room_id, {})
        if not room:
            return

        send_tasks = []
        for websockets in room.values():
            for ws in websockets:
                if ws.client_state.name == "CONNECTED":
                    send_tasks.append(ws.send_json(data))
        
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    async def broadcast_json_to_language(self, room_id, lang, data):
        """Send a realtime event only to listeners of one output language."""
        room = self.active_connections.get(room_id, {})
        websockets = room.get(lang, [])
        send_tasks = [
            ws.send_json(data)
            for ws in websockets
            if ws.client_state.name == "CONNECTED"
        ]
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)

    async def record_subtitle(self, room_id, lang, source_id, text):
        """Store a completed translation and return its public event payload."""
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
        room = self.active_connections.get(room_id, {})
        return [lang for lang, sockets in room.items() if sockets]

    def room_status(self, room_id):
        """Return public room fan-out statistics without exposing connections."""
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
            "room_id": room_id,
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
