# Pulse Flow Feedback Control (MP5Y-25 + NI 9264)

VS Code / Python 데스크톱 UI로 **펄스 유량계 → MP5Y-25(유량 표시) → USB-RS485 → PI + LPF → NI 9264 AO 펌프**를 제어합니다.

## 하드웨어

| 역할 | 장치 | 연결 |
|------|------|------|
| 유량 표시 | Autonics **MP5Y-25** | 펄스 입력 + 프리스케일 **27.6** |
| PC 통신 | USB-RS485 | **COM3**, 9600 8N2, Addr 1 |
| 펌프 AO | NI **9264** `cDAQ2Mod1/ao0` | 0~5 V |

## MP5Y 유량 표시 설정 (확정)

- 펄스정수: **0.46 ml/P**
- 모드: **F1 주파수**
- 프리스케일: **2.76 × 10¹ = 27.6**
- 화면 단위: **cc/min** (`표시값 = Hz × 27.6`)

UI는 MP5Y 화면값(cc/min)을 **그대로 PV로 사용**합니다.  
소프트웨어에서 `×0.46×60`을 다시 하지 않습니다. (이중 환산 방지)

## 배선

### 1) 유량계 → MP5Y-25
펄스 Out / GND → MP5Y 입력 (NPN/PNP 설정 일치)

### 2) MP5Y → USB-RS485 → PC
| MP5Y | 컨버터 |
|------|--------|
| A(+) | A |
| B(−) | B |

### 3) 펌프 → NI 9264
| 펌프 | NI |
|------|-----|
| + | `cDAQ2Mod1/ao0` |
| GND | AO GND |

## 실행

```bash
py -m pip install -r requirements.txt
py pump.py
```

## 화면
1. 유량 모니터 — MP5Y cc/min
2. AO 수동 출력 — 0~5 V
3. 유량 피드백 제어 — SV / P / I / LPF

```text
AO = clamp(P × (SV − PV) + I × ∫(SV − PV) dt, 0, 5 V)
```
