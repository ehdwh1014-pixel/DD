# Pulse Flow Feedback Control (NI 9422 + NI 9264)

VS Code / Python 데스크톱 UI로 **펄스 유량계 → DI 카운터 → 유량 환산 → PI + LPF 피드백 → AO 펌프 전압**을 제어합니다.

## 하드웨어 (NI MAX 기준)

| 슬롯 | 모델 | Device Name | 용도 | 채널 |
|------|------|-------------|------|------|
| Chassis | cDAQ-9178 | `cDAQ2` | 카운터 백플레인 | `ctr0` |
| 1 | NI 9264 | `cDAQ2Mod1` | 펌프 AO 0~5 V | `ao0` |
| 2 | NI 9422 | `cDAQ2Mod2` | 유량계 펄스 카운트 | `PFI0` / **DI0** (첫 채널) |

- 카운터 태스크: `cDAQ2/ctr0`
- 펄스 입력 터미널: `/cDAQ2Mod2/PFI0` (= DI0)
- AO 출력: `cDAQ2Mod1/ao0`

> NI 9425는 cDAQ에서 카운터를 쓸 수 없습니다. 펄스 카운트는 **9422**를 사용합니다.

## 유량계 (확정)

- 모델: **Aichi Tokei OF05ZAT-AR**
- 출력: **AR = 전압 펄스** (외부 풀업 저항 불필요)
- 펄스정수: **0.46 ml/P**
- 사용 유량대: 약 **120 cc/min** → 약 **4.35 Hz** (9422 한도 4 kHz 대비 충분)
- 전원: **12 V 또는 24 V DC**

환산식:

```text
유량(cc/min) = (Δ펄스 / Δt초) × 0.46 × 60
             = 주파수(Hz) × 27.6
```

## 최종 배선 (OF05ZAT-AR ↔ NI 9422 + NI 9264)

AR 타입은 유량계가 스스로 High/Low 전압 펄스를 내보내므로 **풀업 저항 없이** 연결합니다.

### 1) 유량계 → NI 9422 (DI0)

| 유량계 | 연결 |
|--------|------|
| 🔴 Red (+V) | 파워서플라이 **+12V 또는 +24V** |
| ⚫ Black (GND) | 파워서플라이 **GND (−)** |
| ⚪ White (Signal Out) | NI 9422 **DI0+** |
| — | NI 9422 **DI0−** → 파워서플라이 **GND (−)** ⭐ 필수 |

```text
PSU +12/24V ─── Red (유량계)
PSU GND     ──┬─ Black (유량계)
              ├─ DI0− (NI 9422)
              └─ AO GND (NI 9264)

White (유량계 Out) ─── DI0+ (NI 9422)
```

### 2) 펌프 → NI 9264 (ao0)

| 펌프 / AO | 연결 |
|-----------|------|
| 펌프 전압 입력 (+) | NI 9264 **ao0** |
| 펌프 GND / AO COM | 파워서플라이 **GND (−)** 에 공통 |

### 3) 공통 GND (노이즈 방지)

파워서플라이 **마이너스(−)** 한 점에 아래를 **모두** 묶습니다.

1. 유량계 Black  
2. NI 9422 **DI0−**  
3. NI 9264 **AO GND / COM**

기준 전위가 하나로 맞아야 펄스 카운트와 AO 펌프 제어가 안정적입니다.

## 실행 (VS Code)

```bash
python -m pip install -r requirements.txt
python -m flow_ui
# 또는
python flow_ui/app.py
```

NI 드라이버/하드웨어가 없어도 **시뮬레이션 모드**로 UI·그래프·제어 루프를 확인할 수 있습니다.  
실제 장치는 NI-DAQmx 드라이버 + `nidaqmx` 패키지가 필요합니다.

## 화면

1. **유량 모니터** — 펄스 주파수 / cc/min 실시간 추이
2. **AO 수동 출력** — 펌프 전압 0~5 V 수동 테스트
3. **유량 피드백 제어** — SV, **P Gain**, **I Gain**, **LPF** 설정 후 PI 제어
4. **배선 가이드** 버튼 — OF05ZAT-AR 확정 배선 안내

제어식:

```text
AO = clamp(P × (SV − PV) + I × ∫(SV − PV) dt, 0, 5 V)
```

- PV: 펄스 유량 + 1차 로우패스 필터
- 출력 포화 시 적분 anti-windup 적용
- 설정은 `flow_ui/settings.json`에 저장

## 기본 게인 (시작점)

| 항목 | 기본값 | 설명 |
|------|--------|------|
| SV | 120 cc/min | 목표 유량 |
| P Gain | 0.020 V·min/cc | 오차 10 cc → 0.2 V |
| I Gain | 0.005 V·min/cc·s | 정상상태 오차 보정 |
| LPF | 0.8 Hz | 저주파 펄스 지터 완화 |

현장 펌프/배관에 맞춰 P→I 순으로 튜닝하세요.
