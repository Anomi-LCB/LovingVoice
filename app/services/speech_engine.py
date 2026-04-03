import os
import asyncio
import logging
import threading
import base64
import httpx
import json
import io
import wave
from google.cloud import speech, texttospeech
from google import genai

logger = logging.getLogger("LovingVoice.Engine")

class LovingVoiceEngine:
    def __init__(self):
        self.stt = speech.SpeechClient()
        self.tts = texttospeech.TextToSpeechClient()
        self.gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.live_api_key = os.environ.get("GEMINI_LIVE_API_KEY")
        self.http_client = httpx.AsyncClient(timeout=30.0)

    async def transcribe_stream(self, audio_generator):
        """실시간 스트리밍 인식 (LINEAR16 → Google STT)

        gRPC 스트림 전체(요청 전송 + 응답 수신)를 백그라운드 스레드에서 처리.
        결과는 asyncio Queue를 통해 이벤트 루프로 전달.
        """
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="ko-KR",
            model="latest_long",           # 긴 강연/설교에 최적화된 최신 장문 모델
            use_enhanced=True,             # 향상된 음성 인식 모델 사용
            enable_automatic_punctuation=True,
            profanity_filter=False,        # 필터링 없이 발화 원문 100% 수신
            enable_word_confidence=True,   # 단어별 신뢰도 추적
            max_alternatives=1,
            metadata=speech.RecognitionMetadata(
                interaction_type=speech.RecognitionMetadata.InteractionType.DICTATION,
                microphone_distance=speech.RecognitionMetadata.MicrophoneDistance.NEARFIELD,
                recording_device_type=speech.RecognitionMetadata.RecordingDeviceType.PC,
            ),
            speech_contexts=[
                {
                    # 성경 인물 (구약)
                    "phrases": [
                        "노아", "아브라함", "이삭", "야곱", "요셉", "모세", "여호수아", "삼손", "기드온",
                        "사무엘", "다윗", "솔로몬", "엘리야", "엘리사", "이사야", "예레미야", "에스겔",
                        "다니엘", "호세아", "요엘", "아모스", "오바댜", "요나", "미가", "나훔",
                        "하박국", "스바냐", "학개", "스가랴", "말라기", "룻", "에스더", "라합",
                        "사라", "리브가", "라헬", "레아", "한나", "드보라", "미리암",
                        "가인", "아벨", "셋", "에녹", "므두셀라", "라멕", "느헤미야", "에스라", "욥",
                        "아론", "여호사밧", "히스기야", "요시야", "느부갓네살", "발람",
                        # 성경 인물 (신약)
                        "예수", "예수님", "예수 그리스도", "그리스도", "메시아",
                        "베드로", "바울", "요한", "마태", "마가", "누가", "야고보",
                        "안드레", "빌립", "바돌로매", "도마", "시몬", "가룟 유다",
                        "디모데", "디도", "빌레몬", "브리스길라", "아굴라",
                        "막달라 마리아", "마르다", "나사로", "니고데모", "삭개오", "바나바",
                    ],
                    "boost": 20.0
                },
                {
                    # 신학 용어 (개혁주의 중심)
                    "phrases": [
                        "의인", "의로운", "구원", "은혜", "칭의", "성화", "영화",
                        "예정", "예정론", "섭리", "언약", "새 언약", "옛 언약",
                        "속죄", "대속", "보혈", "십자가", "부활", "승천", "재림",
                        "삼위일체", "성부", "성자", "성령", "하나님", "여호와",
                        "복음", "율법", "선지자", "사도", "제자", "회개",
                        "세례", "성찬", "성만찬", "주의 만찬", "말씀", "기도", "찬양", "예배", "설교",
                        "개혁주의", "칼빈", "루터", "츠빙글리", "장로교",
                        "웨스트민스터 신앙고백", "하이델베르크 요리문답", "도르트 신경",
                        "전적 타락", "무조건적 선택", "제한 속죄", "불가항력적 은혜", "성도의 견인",
                        "언약 신학", "하나님의 주권", "오직 믿음", "오직 은혜", "오직 성경",
                        "교리문답", "당대에", "의인이었습니다", "의로운 사람",
                        "천지창조", "타락", "구속", "완성", "종말론", "천년왕국",
                        "교회론", "성례", "직분", "장로", "집사", "목사", "권사",
                    ],
                    "boost": 18.0
                },
                {
                    # 성경 지명 및 성경 책 이름
                    "phrases": [
                        "예루살렘", "갈릴리", "가나안", "에덴", "시내산", "베들레헴", "나사렛",
                        "요단강", "사마리아", "바벨론", "애굽", "앗수르", "니느웨",
                        "소돔", "고모라", "감람산", "겟세마네", "골고다", "갈보리",
                        "다메섹", "안디옥", "에베소", "빌립보", "고린도", "로마", "밧모",
                        "창세기", "출애굽기", "레위기", "민수기", "신명기",
                        "시편", "잠언", "전도서", "아가", "이사야서",
                        "마태복음", "마가복음", "누가복음", "요한복음", "사도행전",
                        "로마서", "고린도전서", "고린도후서", "갈라디아서", "에베소서",
                        "빌립보서", "골로새서", "히브리서", "야고보서", "요한계시록",
                        # 강연 일반
                        "존경하는", "여러분", "오늘", "강연", "시작하겠습니다", "통역", "들리나요",
                        "할렐루야", "아멘", "감사합니다", "축복합니다",
                    ],
                    "boost": 15.0
                },
            ]
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
            single_utterance=False # [P0 수정] 스트림 유지하되 구절 감지 최적화
        )

        result_queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()

        def request_generator():
            chunk_count = 0
            for chunk in audio_generator:
                if cancel_event.is_set():
                    logger.info("[STT] cancel_event is set. Stopping audio feed to Google STT.")
                    break
                chunk_count += 1
                if chunk_count % 10 == 0:
                    logger.info(f"[STT] 오디오 청크 #{chunk_count} ({len(chunk)} bytes) → Google API 전송")
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        def run_stt_in_thread():
            """gRPC 스트리밍 전체를 스레드에서 실행 — 이벤트 루프 블로킹 없음"""
            try:
                responses = self.stt.streaming_recognize(
                    config=streaming_config,
                    requests=request_generator()
                )
                response_count = 0
                for response in responses:
                    response_count += 1
                    if not response.results:
                        continue
                    result = response.results[0]
                    if not result.alternatives:
                        continue
                    transcript = result.alternatives[0].transcript
                    is_final = result.is_final
                    
                    # [P0 상세 로깅] 인식된 텍스트와 최종 여부를 명확히 기록
                    logger.info(f"[STT] #{response_count} 인식: '{transcript}' (final={is_final})")
                    
                    asyncio.run_coroutine_threadsafe(
                        result_queue.put({"transcript": transcript, "is_final": is_final}),
                        loop
                    ).result(timeout=5)
            except Exception as e:
                logger.error(f"[STT] 스트림 에러: {e}")
            finally:
                try:
                    asyncio.run_coroutine_threadsafe(result_queue.put(None), loop).result(timeout=5)
                except Exception:
                    pass

        threading.Thread(target=run_stt_in_thread, daemon=True).start()

        try:
            while True:
                try:
                    # 1.0초 무응답 시 침묵(Silence)으로 간주하고 즉시 번역 트리거
                    result = await asyncio.wait_for(result_queue.get(), timeout=1.0)
                    if result is None:
                        break
                    yield result
                except asyncio.TimeoutError:
                    yield {"is_silence_timeout": True}
        finally:
            # async for 루프가 break 되거나 종료되면 외부 스레드를 깔끔하게 죽임
            cancel_event.set()

    async def _gemini_translate(self, text, target_lang_name):
        """Gemini 2.5 Flash를 이용한 고품질 번역 (의미와 동사 보존 강화)"""
        try:
            translate_prompt = (
                f"Translate the following Korean speech to {target_lang_name} in a natural, spoken/conversational tone. "
                f"CRITICAL: Do not omit verbs or actions. Ensure the core meaning and complete sentence structure are preserved. "
                f"Example: '하나님은 당신을 사랑합니다' should be 'God loves you', NOT 'God, you'. "
                f"Output ONLY the translation:\n{text}"
            )
            response = await asyncio.to_thread(
                self.gemini.models.generate_content,
                model="gemini-2.5-flash",
                contents=translate_prompt,
                config={
                    "temperature": 0.1,
                    "max_output_tokens": 500,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            return response.text.strip()
        except Exception as e:
            logger.error(f"[Gemini Translate] Failed: {e}")
            return text

    async def _gemini_tts(self, text):
        """Gemini 2.5 Flash Preview TTS — 젊고 명쾌한 남성 보이스 (Puck)"""
        try:
            response = await asyncio.to_thread(
                self.gemini.models.generate_content,
                model="gemini-2.5-flash-preview-tts",
                contents=text,
                config={
                    'response_modalities': ['AUDIO'],
                    'speech_config': {
                        'voice_config': {
                            'prebuilt_voice_config': {
                                'voice_name': 'Puck'
                            }
                        }
                    }
                }
            )
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    return part.inline_data.data
        except Exception as e:
            logger.error(f"[Gemini TTS] Failed: {e}")
        return None

    # ── 언어 코드 → 언어 이름 매핑 (클래스 레벨 공유) ──
    LANG_NAME_MAP = {
        "ko-KR": "Korean", "en-US": "English", "en-GB": "English",
        "zh-TW": "Traditional Chinese", "zh-CN": "Simplified Chinese",
        "ja-JP": "Japanese", "fr-FR": "French", "de-DE": "German",
        "es-ES": "Spanish", "pt-BR": "Portuguese",
        "vi-VN": "Vietnamese", "ta-IN": "Tamil", "ne-NP": "Nepali",
        "mn-MN": "Mongolian", "my-MM": "Burmese", "km-KH": "Khmer"
    }

    async def translate_text(self, text, lang):
        """텍스트 번역만 수행 (자막 먼저 전송용)"""
        if lang == "ko-KR":
            return text
        lang_name = self.LANG_NAME_MAP.get(lang, lang)
        return await self._gemini_translate(text, lang_name)

    async def generate_audio(self, text):
        """번역된 텍스트로 음성 합성 후 base64 WAV 반환"""
        pcm_data = await self._gemini_tts(text)
        if pcm_data:
            logger.info(f"[Gemini TTS] 네이티브 음성 생성 성공 - {len(pcm_data)} bytes")
            with io.BytesIO() as wav_io:
                with wave.open(wav_io, 'wb') as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(24000)
                    wav_file.writeframes(pcm_data)
                wav_bytes = wav_io.getvalue()
            return base64.b64encode(wav_bytes).decode('utf-8')

        # Gemini TTS 실패 시 Google Cloud TTS 폴백
        logger.info("[폴백] Google Cloud TTS로 전환합니다.")
        try:
            # 텍스트에서 언어 감지가 어려우므로 en-US 기본
            input_text = texttospeech.SynthesisInput(text=text)
            voice = texttospeech.VoiceSelectionParams(
                language_code="en-US",
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            )
            tts_response = await asyncio.to_thread(
                self.tts.synthesize_speech,
                input=input_text, voice=voice, audio_config=audio_config,
            )
            audio_bytes = tts_response.audio_content
            if audio_bytes and len(audio_bytes) >= 100:
                return base64.b64encode(audio_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"[TTS] 폴백 실패: {e}")

        return ""

    async def generate_audio_fast(self, text, lang="en-US"):
        """⚡ 속도 우선 모드: Google Cloud TTS (Neural2/Studio) — ~0.3초"""
        try:
            # 언어별 최적화된 명쾌한 남성 목소리 선택
            voice_name = None
            if lang.startswith("en"):
                voice_name = "en-US-Neural2-D" # 젊고 명쾌한 남성
            elif lang.startswith("ko"):
                voice_name = "ko-KR-Neural2-C" # 명확한 남성
            
            input_text = texttospeech.SynthesisInput(text=text)
            voice = texttospeech.VoiceSelectionParams(
                language_code=lang,
                name=voice_name,
                ssml_gender=texttospeech.SsmlVoiceGender.MALE if not voice_name else texttospeech.SsmlVoiceGender.SSML_VOICE_GENDER_UNSPECIFIED,
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=1.05, # 약간 빠르게
                pitch=1.0,         # 명쾌한 톤
            )
            tts_response = await asyncio.to_thread(
                self.tts.synthesize_speech,
                input=input_text, voice=voice, audio_config=audio_config,
            )
            audio_bytes = tts_response.audio_content
            if audio_bytes and len(audio_bytes) >= 100:
                logger.info(f"[Speed TTS] {lang} ({voice_name or 'Default'}) 음성 생성 완료")
                return base64.b64encode(audio_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"[Speed TTS] 실패: {e}")
        return ""

    async def translate_and_tts(self, text, lang, gender):
        """(레거시) 텍스트 번역 + 음성 합성 일괄 처리"""
        translated = await self.translate_text(text, lang)
        logger.info(f"[번역] {lang}: '{text[:20]}' → '{translated[:20]}'")
        audio_b64 = await self.generate_audio(translated)
        return {"translated_text": translated, "audio_b64": audio_b64 or ""}
