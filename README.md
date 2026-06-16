# WasteManager — AIoT 스마트 분리수거 시스템

Raspberry Pi 기반 AIoT 분리수거 장치의 통합 제어 프로그램입니다.
초음파 센서로 사용자를 감지해 카메라로 쓰레기를 촬영하고, AI 추론 서버로 분류한 뒤
서보모터로 분리배출 방향을 안내합니다. 동시에 온습도·위생 상태를 모니터링하고
실시간 웹 대시보드로 전체 상태를 시각화합니다.

> Gachon University · IoT26 Spring 2026 Team Project

---

## 주요 기능

- **상태 기계(State Machine) 기반 제어** — `IDLE → SCANNING → SUCCESS/ERROR` 흐름
- **AI 쓰레기 분류** — 촬영 이미지를 추론 서버(`/predict`)로 전송, 신뢰도 기반 판정
- **서보모터 분리배출** — 분류 결과에 따라 판을 기울이고 입구를 개폐
- **환경·위생 모니터링** — DHT11 온습도 → 불쾌지수(DI) → 위생 등급 산출 및 로깅
- **실시간 웹 대시보드** — Flask + Chart.js, 온습도 추이/위험지수/누적 통계/터미널 콘솔
- **로컬 데이터 적재** — SQLite3 (`env_logs`, `waste_logs`)

---

## 하드웨어 구성

GPIO 칩: `/dev/gpiochip4` (Raspberry Pi 5)

| 부품 | 연결 (BCM) | 용도 |
|---|---|---|
| HC-SR04 초음파 | TRIG 23 / ECHO 24 | 사용자 접근 감지(≤ 7cm 트리거) |
| DHT11 온습도 | 17 | 내부 온도·습도 측정 |
| 상태 LED (R/Y/G) | 5 / 12 / 6 | 시스템 상태 표시등 |
| RGB LED | 16 / 20 / 21 | 촬영 조명 |
| 분류 판 서보 | 18 | 분리배출 방향으로 판 기울임 |
| 입구 개폐 서보 | 19 | 투입구 개방/폐쇄 |
| I2C LCD 16x2 | 주소 0x27, bus 1 | 안내 메시지 출력 |
| 카메라 | CSI | `rpicam-still` / `libcamera-still` |

---

## 서보모터 동작 흐름 (각도)

서보는 두 개입니다 — **분류 판 서보(GPIO 18)** 와 **입구 서보(GPIO 19)**.

```
[시작] 분류 서보 0° · 입구 0°
   │  초음파 ≤7cm 감지 → 촬영 → AI 분류
   ▼
[성공] 분류 서보 0° → 분류 각도 ──▶ 입구 → 180° 개방
   │                                    │ (5초 대기)
   ▼                                    ▼
[복귀] 분류 서보 → 0° ──▶ 입구 → 0° 닫힘 ──▶ [IDLE]
```

분류별 판 각도:

| 분류 (`class_name`) | 각도 |
|---|---|
| `plastico` (플라스틱) | 25° |
| `metal` (캔/메탈) | 75° |
| `papel_y_carton` (종이/박스) | 105° |
| `vidrio` / `organico` / 그 외(일반) | 155° |
| 기본 복귀 | 0° |

> 분류 성공 시 오차를 줄이기 위해 **분류 판을 0°로 먼저 보낸 뒤 목표 각도로 이동**하고,
> 그다음 입구를 개방합니다. 복귀 시에는 분류 판을 0°로 되돌린 뒤 입구를 닫습니다.
> SG90 스펙(0° = 0.5ms, 180° = 2.5ms, 50Hz)을 소프트웨어 PWM으로 구동하며,
> 입력 각도는 0~180° 범위로 자동 보정됩니다.

---

## 파일 구성

| 파일 | 설명 |
|---|---|
| `main_final.py` | **메인 통합 프로그램** (센서 + 서보 + LED + LCD + SQLite3 + Flask 대시보드) |
| `servo_test.py` | 서보모터 단독 테스트 도구 (verbose 로그, 대화형/직접 실행) |
| `main.py` | 대시보드·DB 이전의 초기 버전 (센서·서보 제어 위주) |
| `design.md` | 대시보드 디자인 시스템 토큰 (Linear 다크 테마) 레퍼런스 |

---

## 실행 방법

### 의존성

```bash
pip install flask requests gpiod smbus2
```
추가로 카메라 캡처에 `rpicam-still` 또는 `libcamera-still` 가 필요합니다.

### 메인 프로그램 실행

```bash
python3 main_final.py
```

- AI 추론 서버가 `http://localhost:8000/predict` 에서 대기 중이어야 합니다 (`API_URL`).
- 실행 시 SQLite3 DB(`smart_recycling.db`)가 자동 생성됩니다.
- 웹 대시보드: 같은 네트워크에서 `http://<라즈베리파이 IP>:5000`

### 서보모터 테스트

```bash
# 대화형 메뉴 (서보 선택 → 동작 선택)
python3 servo_test.py

# 직접 실행: pin angle [duration]
python3 servo_test.py 18 0         # 분류 판 → 0°
python3 servo_test.py 19 180 1.6   # 입구 → 180°, 1.6초
```

---

## 웹 대시보드

Flask 서버(포트 5000)가 백그라운드 스레드로 구동되며, 단일 화면에 다음을 표시합니다.

- **상단 카드 4개** — 온도 / 습도(꺾은선 추이) · 위생 위험 지수(0–100) · 위생 상태 등급(A~D)
- **하단 3열** — 실시간 배출 로그 · 누적 배출 히스토그램(4분류) · 서버 터미널 콘솔

### REST API

| 엔드포인트 | 설명 |
|---|---|
| `GET /` | 대시보드 HTML |
| `GET /api/status` | 최신 환경·위험지수, 최근 배출 이력, 누적 통계 (JSON) |
| `GET /api/console_log?since=N` | `N` 인덱스 이후의 서버 콘솔 로그 증분 스트림 |

> Chart.js·웹폰트는 CDN을 사용하므로 대시보드 차트 렌더링에는 인터넷 연결이 필요합니다.

---

## 데이터베이스 스키마 (`smart_recycling.db`)

**env_logs** — 환경/위생 로그
`id, timestamp, temperature, humidity, discomfort_index, hygiene_status`

**waste_logs** — 배출 분류 이력
`id, timestamp, waste_type, servo_angle, confidence`

위생 등급은 불쾌지수(DI)와 온습도 임계치로 산출되며, 대시보드에서는 DI를 0–100 위험지수로
변환해 A(양호)/B(주의)/C(경고)/D(위험) 등급으로 표시합니다.
