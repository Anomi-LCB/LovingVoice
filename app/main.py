from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
import logging
import os
import asyncio
from dotenv import load_dotenv
from app.services.speech_engine import LovingVoiceEngine
from app.services.connection import ConnectionManager

# .env 파일 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LovingVoice")

app = FastAPI(title="LovingVoice API")

# 엔진 및 매니저 초기화
engine = LovingVoiceEngine()
manager = ConnectionManager()

@app.get("/")
async def get_index():
    try:
        with open("app/templates/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: app/templates/index.html missing</h1>", status_code=404)

@app.get("/recorder-worklet.js")
async def get_worklet():
    return FileResponse("app/templates/audio-processor.js", media_type="application/javascript")

import queue
import threading

@app.websocket("/ws/speaker/{room_id}")
async def speaker_endpoint(websocket: WebSocket, room_id: str):
    # 쿼리 파라미터에서 비밀번호 및 모드 추출
    password = websocket.query_params.get("password")
    voice_mode = websocket.query_params.get("voice_mode", "speed")
    
    # [인증 1] 비밀번호 검증
    correct_pass = os.getenv("SPEAKER_PASSWORD", "loving77")
    if password != correct_pass:
        logger.warning(f"Speaker connection rejected: Invalid password for room {room_id}")
        await websocket.accept() # 클라이언트에 에러를 보내기 위해 일단 수락
        await websocket.close(code=4001, reason="Invalid speaker password")
        return

    # [인증 2] 입중 인원 제한 추가
    if not await manager.add_speaker(room_id, websocket):
        logger.warning(f"Speaker connection rejected: Room {room_id} is full.")
        await websocket.accept()
        await websocket.close(code=4002, reason="Speaker room is full (max 2)")
        return

    await websocket.accept()
    manager.voice_modes = getattr(manager, 'voice_modes', {})
    manager.voice_modes[room_id] = voice_mode
    logger.info(f"Speaker connected to room: {room_id} (voice_mode={voice_mode})")
    
    # 오디오 데이터를 담을 비동기 큐
    audio_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()  # [P2 수정] get_event_loop() deprecated → get_running_loop()
    
    def audio_generator():
        """비동기 큐에서 데이터를 가져와 gRPC 스트림으로 전달하는 브릿지"""
        while True:
            # 스레드에서 비동기 큐의 데이터를 안전하게 가져옴
            future = asyncio.run_coroutine_threadsafe(audio_queue.get(), loop)
            try:
                chunk = future.result() # 데이터가 올 때까지 블로킹
                if chunk is None:
                    logger.info(f"[{room_id}] Audio generator closing...")
                    return
                # 데이터 유입 확인용 로그 (데이터가 실제로 흐르는지 체크)
                logger.debug(f"[{room_id}] Yielding {len(chunk)} bytes to STT generator")
                yield chunk
            except Exception as e:
                logger.error(f"[{room_id}] Audio generator error: {e}")
                return

    async def stt_processor():
        """STT 연동 및 실시간 결과 처리 루프 (불사조 시스템: 무한 재시도)"""
        while True:
            try:
                last_transcript = ""
                # [P0 개선] 문장이 너무 길어지면(강제 발화) 중복 전송 방지를 위한 자체 1.2초 무음 감지
                async for result in engine.transcribe_stream(audio_generator()):
                    if result.get("is_silence_timeout"):
                        if last_transcript and len(last_transcript) >= 5:
                            logger.info(f"[{room_id}] Silence Timeout → Translating: {last_transcript}")
                            try:
                                await manager.broadcast_to_room(room_id, last_transcript, engine)
                            except Exception as e:
                                logger.error(f"[{room_id}] Broadcast failed in main loop: {e}")
                            # 강제 번역 즉시, 백그라운드 STT 접속을 끊어버리고 새롭게 시작하여 중복 누적을 원천 차단
                            break
                        # 5글자 미만의 짧은 조각은 아직 문장이 완성되지 않은 것으로 판단 → 계속 대기
                        continue

                    transcript = result["transcript"].strip()
                    if not transcript or len(transcript) < 2: continue
                    last_transcript = transcript
                    
                    is_final = result["is_final"]
                    
                    # 실시간 자막 발송 (청중 반응성)
                    msg = {"type": "transcript", "text": transcript, "is_final": is_final}
                    if websocket.client_state.name == "CONNECTED":
                        await websocket.send_json(msg)
                    await manager.broadcast_json_to_room(room_id, msg)
                    
                    if is_final:
                        logger.info(f"[{room_id}] Final Recognized: {transcript}")
                        try:
                            # 번역 및 음성 전송
                            await manager.broadcast_to_room(room_id, transcript, engine)
                        except Exception as e:
                            logger.error(f"[{room_id}] Broadcast failed in main loop: {e}")
                        # 구글 API가 정상적으로 문장 완성을 주더라도, 깔끔함을 위해 스트림 갱신
                        break
                
                # 정상 종료 시 (예: Google 5분 제한) 즉시 재시작 루프로 진입
                logger.info(f"[{room_id}] STT Stream timeout/closed. Restarting phoenix system...")
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"[{room_id}] STT Phoenix error: {e}. Recovering in 1s...")
                await asyncio.sleep(1)
                if not websocket.client_state.name == "CONNECTED":
                    break

    # STT 프로세서를 별도 태스크로 실행
    stt_task = asyncio.create_task(stt_processor())

    try:
        while True:
            # 바이너리 또는 텍스트 수신 처리 (하트비트 대응)
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                if "bytes" in data:
                    audio_bytes = data["bytes"]
                    await audio_queue.put(audio_bytes)
                elif "text" in data:
                    try:
                        import json
                        msg = json.loads(data["text"])
                        if msg.get("type") == "test_audio":
                            test_text = "🔔 강연자가 오디오 장치를 테스트 중입니다. (Test Bell)"
                            logger.info(f"[{room_id}] Test audio requested by speaker. Broadcasting...")
                            # 청중에게 즉시 텍스트와 테스트 알림 전송
                            await manager.broadcast_json_to_room(room_id, {"type": "transcript", "text": test_text, "is_final": True})
                            # TTS 엔진을 통한 실제 벨소리/음성 테스트 시도
                            await manager.broadcast_to_room(room_id, "Hello, this is a test audio for Loving Voice speaker connection.", engine)
                    except Exception as e:
                        logger.error(f"[{room_id}] Error processing text message: {e}")
            except asyncio.TimeoutError:
                # 30초간 데이터 없으면 연결 상태 확인용 핑 테스트 가능
                continue
    except WebSocketDisconnect:
        logger.info(f"Speaker disconnected from room: {room_id}")
        manager.remove_speaker(room_id, websocket)
    except Exception as e:
        logger.error(f"Speaker error: {e}")
        manager.remove_speaker(room_id, websocket)
    finally:
        # 종료 전 태스크 및 큐 정리
        stt_task.cancel()
        try:
            await stt_task
        except asyncio.CancelledError:
            pass
        await audio_queue.put(None) 
        logger.info(f"[{room_id}] Speaker cleanup complete.")

@app.websocket("/ws/audience/{room_id}/{lang}")
async def audience_endpoint(websocket: WebSocket, room_id: str, lang: str):
    await manager.add_audience(room_id, lang, websocket)
    logger.info(f"Audience connected to room: {room_id}, lang: {lang}")
    try:
        while True:
            # 전송 전용이므로 하트비트(ping)만 수신 대기
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info(f"Audience disconnected from room: {room_id}, lang: {lang}")
        manager.remove_audience(room_id, lang, websocket)
    except Exception as e:
        logger.error(f"Audience error in room {room_id}: {e}")
        manager.remove_audience(room_id, lang, websocket)