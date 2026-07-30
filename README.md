# NI-DAQ 제어 UI

Python + VS Code에서 실행하는 NI-9206 / NI-9264 시험 및 피드백 제어용 데스크톱 UI입니다.

## 실행

```bash
python nidaq_ui/app.py
```

NI 하드웨어 없이도 화면과 그래프를 확인할 수 있습니다. 이 경우 앱은 시뮬레이션 입력값을 보여 주고 통신 상태는 빨간색으로 표시합니다.

실제 NI-DAQ에서 사용하려면 NI-DAQmx 드라이버 설치 후 다음을 실행합니다.

```bash
python -m pip install -r requirements.txt
python nidaq_ui/app.py
```

## 하드웨어 (NI MAX 기준)

| 슬롯 | 모델 | Device Name | Serial |
| --- | --- | --- | --- |
| 2 | NI 9206 | `cDAQ1Mod2` | 01F4D2D3 |
| 3 | NI 9264 | `cDAQ1Mod3` | 01FA4B72 |

첫 번째 모듈/슬롯은 다른 용도로 사용 중이므로 비워 둡니다.

## 기본 채널 배정

두 번째 채널(DAQmx 인덱스 `1`)은 AI/AO 시험, 세 번째 채널(인덱스 `2`)은 피드백 제어입니다.

| 기능 | DAQmx 채널 |
| --- | --- |
| AI TEST (NI 9206) | `cDAQ1Mod2/ai1` |
| AO TEST (NI 9264) | `cDAQ1Mod3/ao1` |
| Feedback PV (NI 9206) | `cDAQ1Mod2/ai2` |
| Feedback AO (NI 9264) | `cDAQ1Mod3/ao2` |

## 화면 기능

- 상단 상태등: NI-DAQmx 장치가 감지되면 녹색 점등/점멸, 아니면 빨간색
- **AI TEST**: 0–5 V 입력 트렌드와 유량 또는 압력 범위 선형 환산
- **AO TEST**: 0–5 V 수동 출력 및 출력 트렌드
- **피드백 제어**: AI 전압을 유량 또는 압력으로 환산한 PV/SV 트렌드, AO 출력 트렌드, PI 제어 및 1차 로우패스 필터

## 피드백 제어

피드백 제어 화면에서 아래 값을 개별적으로 설정하고 **설정 저장**으로 다음 실행에도 유지할 수 있습니다.

- AI 전압 범위와 이를 환산할 유량 또는 압력 공정 범위
- SV (유량 또는 압력 기준)
- P Gain (`V / 공정값`), I Gain (`V / 공정값·s`)
- 로우패스 필터 (Low Pass Filter) 차단 주파수 (Hz, `0`이면 필터 미사용)

제어식은 `AO = clamp(P × (SV − PV) + I × ∫(SV − PV)dt, 0, 5 V)`입니다. PV는 AI 전압을 공정값으로 선형 환산한 뒤 **1차 로우패스 필터**로 노이즈를 줄인 값을 사용합니다. 출력 포화 시 적분값이 계속 쌓이지 않도록 anti-windup을 적용합니다.
