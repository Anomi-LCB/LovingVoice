import asyncio
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # 구조: {room_id: {lang: [websocket, websocket]}}
        self.active_connections = {}
        # 강연자 관리: {room_id: [websocket]}
        self.active_speakers = {}

    async def add_speaker(self, room_id, websocket):
        """방에 강연자 추가 (최대 2명 제한)"""
        if room_id not in self.active_speakers:
            self.active_speakers[room_id] = []
        
        if len(self.active_speakers[room_id]) >= 2:
            return False # 입장 거부
            
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

    async def add_audience(self, room_id, lang, websocket):
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        if lang not in self.active_connections[room_id]:
            self.active_connections[room_id][lang] = []
        self.active_connections[room_id][lang].append(websocket)
        await websocket.accept()

    def remove_audience(self, room_id, lang, websocket):
        try:
            if room_id in self.active_connections and lang in self.active_connections[room_id]:
                self.active_connections[room_id][lang].remove(websocket)
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
            await asyncio.gather(*send_tasks)