# LovingVoice 제품·기술 비전

## 제품 정체성

LovingVoice는 단순한 AI 번역기가 아니라 **한 사람의 음성을 현장의 모든 사람이 자기 언어로 함께 듣는 다국어 강연 운영체제**다. 핵심 약속은 세 가지다.

1. 강연자는 한 번만 말한다.
2. 같은 언어의 청중은 하나의 실시간 번역 경로를 공유한다.
3. 청중은 현재 자막, 최근 맥락, 번역 음성을 지연 없이 받는다.

현재 강연 전송 구조는 OpenAI가 안내하는 listen-along 패턴과 같은 방향이다. 기본 13개 출력 언어는 전용 `gpt-realtime-translate` 모델과 `/v1/realtime/translations` 경로를 사용하고, 목표 언어별 세션 하나를 공유한다. 전용 모델의 출력 목록에 아직 없는 타갈로그어는 OpenAI의 Realtime API 통역 패턴에 따라 `gpt-realtime-2.1` 전용 세션으로 보완한다.

- [OpenAI Realtime translation guide](https://developers.openai.com/api/docs/guides/realtime-translation)
- [OpenAI listen-along reference architecture](https://developers.openai.com/cookbook/examples/voice_solutions/realtime_translation_guide)
- [OpenAI Realtime API one-way translation pattern](https://developers.openai.com/cookbook/examples/voice_solutions/one_way_translation_using_realtime_api)

## 이번에 적용한 1차 혁신 패키지

- **Universal Broadcast Input**: 현장 마이크뿐 아니라 브라우저 탭·시스템 오디오를 직접 입력받는다. 온라인 설교, 영상, 원격 회의도 같은 언어별 공유 경로를 사용한다.
- **현장 지속 모드**: 방송·청취 중 Screen Wake Lock을 사용해 휴대폰과 태블릿 화면이 잠들어 자막·오디오가 끊기는 문제를 줄인다.
- **Live Quality HUD**: 청중 기기에서 서버 왕복시간과 강연 음성부터 첫 번역 음성이 도착할 때까지의 체감 지연을 측정한다.
- **개인정보 최소 안전 식별자**: 이름·이메일 대신 방과 음원 세션을 단방향 해시한 식별자를 OpenAI 세션에 전달한다.
- **오디오 전용 로컬 녹음**: 탭 공유를 선택해도 비디오 트랙을 서버나 녹음 파일에 넣지 않고 오디오와 강연자 자막만 저장한다.
- **운영 비밀 분리**: OpenAI·Gemini 키와 방 서명 비밀은 Cloud Run 일반 환경변수 값이 아니라 Google Secret Manager의 고정 버전으로 주입한다.
- **1인 동시통역**: 방이나 두 번째 기기 없이 한 WebSocket에서 마이크 입력, 번역 자막, 번역 음성을 함께 처리한다. 언어 뒤바꾸기는 목표 언어 세션을 교체하고 이전 음성 대기열을 비워 현지 대화를 빠르게 왕복한다.
- **같은 기기 에코 보호**: 마이크에 브라우저 에코 제거·소음 억제·자동 게인을 적용하고, 이어폰 사용 시 말이 끝나기 전부터 시작되는 초저지연 번역을 가장 안정적으로 유지한다.

## 반드시 해결할 기반 구조

현재 방, 인증, 청중 연결, 자막 기록, 번역 세션이 한 Cloud Run 인스턴스의 메모리에 있다. `maxScale=1`에서는 언어당 한 경로가 정확히 보장되지만 배포나 장애 때 방이 사라지고, 인스턴스를 늘리면 같은 방이 서로 다른 서버로 갈라질 수 있다. Cloud Run의 WebSocket 세션 선호도는 최선 노력 방식이므로, 다중 인스턴스 운영에는 외부 상태와 동기화 계층이 필요하다.

- [Cloud Run WebSocket 운영 지침](https://docs.cloud.google.com/run/docs/triggering/websockets)
- [Cloud Run 동시성 설정](https://docs.cloud.google.com/run/docs/about-concurrency)

### 2단계: 전 세계 확장 가능한 공유 통역 평면

```text
브라우저/행사 음원
        │
        ▼
Stateless Gateway ── 방 인증·상태 ── Redis/Firestore
        │
        ▼
Room Media Owner ── 분산 lease: room/source/language
        │                   │
        │                   └─ 목표 언어당 OpenAI 세션 정확히 1개
        ▼
Redis Streams 또는 SFU fan-out
        │
        └─ 여러 Cloud Run 인스턴스의 모든 청중
```

- 방 정보·비밀번호 해시·호스트 권한은 Firestore 같은 내구성 저장소에 둔다.
- `room/source/language` 키의 분산 lease를 잡은 미디어 워커만 OpenAI 세션을 만든다.
- 자막 이벤트는 Redis Pub/Sub, 짧은 재연결 기록은 Redis Streams로 전파한다.
- 다수 청중의 원음·번역 음성 전송은 Redis에 큰 오디오를 반복 저장하기보다 WebRTC SFU 또는 전용 미디어 fan-out 계층을 사용한다.
- 게이트웨이는 무상태로 확장하고, 미디어 소유권은 장애 시 짧은 lease 만료 후 다른 워커가 이어받는다.
- 이 단계가 완료되어야 `maxScale`을 안전하게 늘리면서도 **언어당 한 번만 과금**을 유지할 수 있다.

## 다음 혁신 후보

### Living Context

청중에게 전체 과거 자막을 보내지 않고 현재 주제, 핵심 인명·용어, 직전 2~3문장의 의미만 담은 작은 맥락 캡슐을 언어별로 한 번 생성한다. 늦게 들어온 사람은 5초 안에 강연 흐름을 이해하고, 네트워크와 토큰 사용량은 거의 늘지 않는다.

### Meaning Check

숫자, 날짜, 성경 구절, 고유명사, 부정 표현처럼 틀리면 의미가 크게 바뀌는 항목을 원문 자막과 번역 자막에서 자동 대조한다. 불확실한 항목만 조용히 밑줄로 표시하고 강연자 콘솔에는 사후 검토 목록을 만든다. 번역을 느리게 만드는 전면 재검증 대신 위험 구간만 선택적으로 검사한다.

### Interpretation Twin

실제 개인정보를 저장하지 않는 합성 강연 세트를 만들어 배포마다 자동 재생한다. 첫 자막 시간, 첫 음성 시간, 누락률, 숫자·이름 보존율, 20줄 자막 승격 규칙을 동일하게 측정해 “코드상 성공”이 아니라 “강연 현장에서 성공”을 출시 기준으로 삼는다.

### Venue Continuity

QR·NFC·PWA를 결합해 좌석에서 스캔하면 즉시 자신의 언어가 복원되고, 네트워크가 순간 끊겨도 최근 자막과 다음 오디오가 자연스럽게 이어진다. 행사장 와이파이 품질이 나쁘면 Live Quality HUD가 원음 보조 또는 자막 우선 모드를 권한다.

### Audio Source Matrix

마이크·탭 오디오 다음에는 USB 믹서, 휴대폰 원격 마이크, SRT/RTMP 행사 음원을 하나의 입력 매트릭스로 묶는다. 소스별 음량·잡음 상태를 표시하고, 진행자와 영상 음원이 바뀌어도 같은 방과 언어 경로를 유지한다.

## 출시 판단 지표

- 서버 왕복시간 p95와 강연 음성→첫 번역 음성 p50/p95
- 첫 자막 시간과 첫 음성 시간의 간격
- 세션 재연결 성공률 및 중복 언어 세션 0건
- 30분 강연의 오디오 누락률과 자막 누락률
- 숫자·날짜·고유명사 보존율
- 청중 1명, 100명, 1,000명일 때 언어별 OpenAI 세션 수가 동일한지
- 배포·인스턴스 장애 후 방과 자막 맥락 복구 시간

## 권장 실행 순서

1. 현장 품질 수치를 1~2주 수집해 실제 병목을 구분한다.
2. 내구성 방 저장소와 분산 lease를 구축한다.
3. Redis/SFU 기반 다중 인스턴스 fan-out을 부하 시험한다.
4. Living Context와 Meaning Check를 작은 실험 플래그로 출시한다.
5. Interpretation Twin의 자동 품질 기준을 통과한 빌드만 정식 배포한다.
