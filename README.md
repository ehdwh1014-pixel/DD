# Pulse Flow Feedback Control (MP5Y-25 + NI 9264)

VS Code / Python 데스크톱 UI로 **펄스 유량계 → MP5Y-25 → USB-RS485(Modbus) → 유량 PV → PI + LPF → NI 9264 AO 펌프**를 제어합니다.

## 하드웨어

| 역할 | 장치 | 연결 |
|------|------|------|
| 유량 표시/환산 | Autonics **MP5Y-25** | 펄스 유량계 입력 + RS485 |
| PC 통신 | USB-RS485 컨버터 | **COM3**, 9600 8N2, Addr 1 |
| 펌프 AO | NI **9264** `cDAQ2Mod1/ao0` | 0~5 V |

> NI 9422로 직접 카운트하지 않습니다. 유량은 MP5Y Modbus PV를 사용합니다.

## 유량계

- 펄스정수: **0.46 ml/P**
- MP5Y 권장 모드: **F1 주파수**
- 환산: `유량(cc/min) = Hz × 0.46 × 60`

MP5Y에서 프리스케일로 cc/min을 이미 표시한다면, UI **채널 설정**에서 표시모드를 `flow_ccpm`으로 바꾸세요.

## 배선

### 1) 유량계 → MP5Y-25

펄스 Out / GND를 MP5Y 입력 단자에 연결하고, NPN/PNP 입력 설정을 유량계에 맞춥니다.

### 2) MP5Y-25 → USB-RS485 → PC

| MP5Y | 컨버터 |
|------|--------|
| A(+) | A |
| B(−) | B |

PC 장치관리자에서 COM 포트가 **COM3**인지 확인하세요. (다르면 UI 채널 설정에서 변경)

통신 기본값(MP5Y 출하):

- Baud 9600
- Data 8 / Parity None / Stop **2**
- Address **1**

### 3) 펌프 → NI 9264

| 펌프 | NI |
|------|-----|
| 전압 입력 (+) | `cDAQ2Mod1/ao0` |
| GND / COM | AO GND |

## 실행 (VS Code)

```bash
py -m pip install -r requirements.txt
py pump.py
```

COM3 또는 NI가 없어도 **시뮬레이션 모드**로 UI를 확인할 수 있습니다.

## 화면

1. **유량 모니터** — MP5Y PV / 주파수 추이
2. **AO 수동 출력** — 펌프 0~5 V 테스트
3. **유량 피드백 제어** — SV, P Gain, I Gain, LPF

제어식:

```text
AO = clamp(P × (SV − PV) + I × ∫(SV − PV) dt, 0, 5 V)
```

설정은 `flow_ui/settings.json`에 저장됩니다.

## 기본 게인

| 항목 | 기본값 |
|------|--------|
| SV | 120 cc/min |
| P Gain | 0.020 V·min/cc |
| I Gain | 0.005 V·min/cc·s |
| LPF | 0.8 Hz |
