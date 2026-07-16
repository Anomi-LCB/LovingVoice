from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import logging
import os
import asyncio
import re
import uuid
from urllib.parse import quote
from dotenv import load_dotenv
from app.services.speech_engine import LovingVoiceEngine
from app.services.connection import ConnectionManager
from app.services.realtime_translation import RealtimeTranslationHub
from app.services.room_auth import RoomTokenManager

# .env 파일 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LovingVoice")

app = FastAPI(title="LovingVoice API")

# OpenAI Realtime is the default path. Legacy Google/Gemini clients are created
# lazily so the app can start with only OPENAI_API_KEY configured.
legacy_engine = None
manager = ConnectionManager()
room_tokens = RoomTokenManager()
realtime_hub = RealtimeTranslationHub(
    manager.broadcast_json_to_room,
    manager.broadcast_json_to_language,
    manager.record_subtitle,
)


def get_legacy_engine():
    global legacy_engine
    if legacy_engine is None:
        legacy_engine = LovingVoiceEngine()
    return legacy_engine


@app.on_event("shutdown")
async def shutdown_realtime_hub():
    await realtime_hub.close()

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


@app.get("/app.css")
async def get_app_styles():
    return FileResponse("app/templates/tailwind.css", media_type="text/css")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "realtime_available": realtime_hub.available,
        "realtime_model": realtime_hub.model,
        "realtime_mode": "max_performance",
        "active_translation_sessions": realtime_hub.active_session_count,
    }


@app.get("/api/rooms/{room_id}")
async def room_status(room_id: str):
    status = manager.room_status(room_id)
    status["exists"] = room_tokens.room_exists(room_id)
    status.update(
        {
            "translation_routes": realtime_hub.route_status(
                manager.resolve_room_id(room_id)
            ),
            "route_policy": "one_per_source_channel_and_language",
        }
    )
    return status


class RoomCreateRequest(BaseModel):
    room_id: str
    room_password: str


class RoomUpdateRequest(BaseModel):
    speaker_token: str
    room_id: str
    room_password: str


def normalize_room_id(value: str) -> str:
    normalized = re.sub(r"\s+", "-", value.strip())
    normalized = "".join(
        character
        for character in normalized
        if character.isalnum() or character in "-_"
    )
    if not 2 <= len(normalized) <= 40:
        raise HTTPException(
            status_code=422,
            detail="방 이름은 문자·숫자·하이픈으로 2~40자 이내여야 합니다.",
        )
    return normalized


@app.post("/api/rooms")
async def create_room(request: RoomCreateRequest):
    """Register a user-selected room name/password and issue a host token."""
    room_id = normalize_room_id(request.room_id)
    room_password = request.room_password.strip()
    if not 4 <= len(room_password) <= 32:
        raise HTTPException(
            status_code=422,
            detail="방 비밀번호는 4~32자로 입력하세요.",
        )
    try:
        room = room_tokens.create_room(room_id, room_password)
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail="이미 사용 중인 방 이름입니다.",
        ) from error
    return {
        **room,
        "audience_path": f"/?room={quote(room_id)}&mode=audience",
    }


@app.patch("/api/rooms/{current_room_id}")
async def update_room(current_room_id: str, request: RoomUpdateRequest):
    """Update a live room's public name and audience password."""
    if not room_tokens.verify_speaker_token(
        current_room_id, request.speaker_token
    ):
        raise HTTPException(status_code=403, detail="방 설정 변경 권한이 없습니다.")

    new_room_id = normalize_room_id(request.room_id)
    new_password = request.room_password.strip()
    if not 4 <= len(new_password) <= 32:
        raise HTTPException(
            status_code=422,
            detail="방 비밀번호는 4~32자로 입력하세요.",
        )
    try:
        room = room_tokens.update_room(
            current_room_id, new_room_id, new_password
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="방을 찾을 수 없습니다.") from error
    except ValueError as error:
        raise HTTPException(
            status_code=409, detail="이미 사용 중인 방 이름입니다."
        ) from error

    if new_room_id != current_room_id:
        manager.register_room_alias(current_room_id, new_room_id)
    return {
        **room,
        "audience_path": f"/?room={quote(new_room_id)}&mode=audience",
    }

import queue
import threading

@app.websocket("/ws/speaker/{room_id}")
async def speaker_endpoint(websocket: WebSocket, room_id: str):
    # Engine preferences are harmless URL metadata. Credentials are deliberately
    # received after connection so passwords/tokens never appear in access logs.
    voice_mode = websocket.query_params.get("voice_mode", "speed")
    translation_engine = websocket.query_params.get("engine", "openai")

    await websocket.accept()
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=8)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await websocket.close(code=4001, reason="Speaker authentication required")
        return
    if auth_message.get("type") != "authenticate" or not room_tokens.verify_speaker_token(
        room_id, str(auth_message.get("token", ""))
    ):
        logger.warning("Speaker connection rejected for room %s", room_id)
        await websocket.close(code=4001, reason="Invalid speaker credential")
        return

    await manager.add_speaker(room_id, websocket)
    source_id = uuid.uuid4().hex
    manager.voice_modes = getattr(manager, 'voice_modes', {})
    manager.voice_modes[manager.resolve_room_id(room_id)] = voice_mode
    logger.info(f"Speaker connected to room: {room_id} (voice_mode={voice_mode})")
    await websocket.send_json({"type": "authenticated", "room_id": room_id})
    await manager.broadcast_json_to_room(
        room_id, {"type": "room_status", **manager.room_status(room_id)}
    )

    if translation_engine == "openai":
        if not realtime_hub.available:
            await websocket.send_json(
                {
                    "type": "engine_error",
                    "message": "OPENAI_API_KEY가 설정되지 않았습니다.",
                }
            )
            await websocket.close(code=4003, reason="OpenAI API key is missing")
            manager.remove_speaker(room_id, websocket)
            return

        await websocket.send_json(
            {
                "type": "engine_status",
                "state": "online",
                "message": f"{realtime_hub.model} 준비 완료",
            }
        )
        try:
            while True:
                data = await websocket.receive()
                if data.get("type") == "websocket.disconnect":
                    break
                if data.get("bytes") is not None:
                    languages = manager.realtime_languages(room_id)
                    await realtime_hub.feed(
                        room_id, source_id, data["bytes"], languages
                    )
                elif data.get("text"):
                    try:
                        import json

                        message = json.loads(data["text"])
                        if message.get("type") == "test_audio":
                            test_text = "🔔 LovingVoice 실시간 연결 테스트가 완료되었습니다."
                            for language in manager.audience_languages(room_id):
                                caption = await manager.record_subtitle(
                                    room_id, language, source_id, test_text
                                )
                                await manager.broadcast_json_to_language(
                                    room_id,
                                    language,
                                    {
                                        "type": "translation_delta",
                                        "text": test_text,
                                        "source_id": source_id,
                                    },
                                )
                                await manager.broadcast_json_to_language(
                                    room_id,
                                    language,
                                    {
                                        "type": "translation_done",
                                        "source_id": source_id,
                                        "caption": caption,
                                    },
                                )
                    except ValueError:
                        logger.warning("Invalid speaker control message in room %s", room_id)
        except WebSocketDisconnect:
            logger.info("Realtime speaker disconnected from room: %s", room_id)
        except Exception as exc:
            logger.exception("Realtime speaker error in room %s", room_id)
            if websocket.client_state.name == "CONNECTED":
                await websocket.send_json(
                    {"type": "engine_error", "message": str(exc)}
                )
        finally:
            manager.remove_speaker(room_id, websocket)
            await realtime_hub.close_speaker(room_id, source_id)
            await manager.broadcast_json_to_room(
                room_id, {"type": "room_status", **manager.room_status(room_id)}
            )
        return

    engine = get_legacy_engine()

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
    await websocket.accept()
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=8)
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await websocket.close(code=4001, reason="Room password required")
        return
    if auth_message.get("type") != "authenticate" or not room_tokens.verify_room_password(
        room_id, str(auth_message.get("password", ""))
    ):
        logger.warning("Audience authentication rejected for room %s", room_id)
        await websocket.close(code=4001, reason="Invalid room password")
        return

    await manager.add_audience(room_id, lang, websocket, accept=False)
    await websocket.send_json(
        {
            "type": "history_snapshot",
            "language": lang,
            "items": manager.get_subtitle_history(room_id, lang),
            "policy": {
                "max_items": manager.history_limit,
                "max_age_seconds": manager.history_max_age_seconds,
                "max_bytes": manager.history_max_bytes,
            },
        }
    )
    await manager.broadcast_json_to_room(
        room_id,
        {
            "type": "room_status",
            **manager.room_status(room_id),
            "shared_route": True,
        },
    )
    logger.info(f"Audience connected to room: {room_id}, lang: {lang}")
    try:
        while True:
            # 전송 전용이므로 하트비트(ping)만 수신 대기
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info(f"Audience disconnected from room: {room_id}, lang: {lang}")
        manager.remove_audience(room_id, lang, websocket)
        await manager.broadcast_json_to_room(
            room_id, {"type": "room_status", **manager.room_status(room_id)}
        )
    except Exception as e:
        logger.error(f"Audience error in room {room_id}: {e}")
        manager.remove_audience(room_id, lang, websocket)
        await manager.broadcast_json_to_room(
            room_id, {"type": "room_status", **manager.room_status(room_id)}
        )
