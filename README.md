# Pulse Flow Feedback Control (MP5Y-25 + NI 9264)

VS Code / Python 데스크톱 UI로 **펄스 유량계 → MP5Y-25(유량 표시) → USB-RS485 → PI + LPF → NI 9264 AO 펌프**를 제어합니다.

## 하드웨어

| 역할 | 장치 | 연결 |
|------|------|------|
| 유량 표시 | Autonics **MP5Y-25** | 펄스 입력 + 프리스케일 **27.6** |
| PC 통신 | USB-RS485 | **COM3**, 9600 8N2, Addr 1 |
| 펌프 AO | NI **9264** `cDAQ2Mod1/ao0` | **REF.W** 0~5 V → 물펌프/인버터 V1 |
| NG 펌프 AO | NI **9264** `cDAQ2Mod1/ao1` | **NG PUMP** 수동 0~5 V |
| 여유 AO | NI **9264** `cDAQ2Mod1/ao2` | **AO 2** 수동 0~5 V |
| 레벨 DI | NI **9422** `cDAQ2Mod2/port0/line0:5` | HIGH/LOW × 3 |
| 밸브 DO | NI **9477** `cDAQ2Mod3/port0/line0:2` | 싱킹 출력 × 3 |
| 화염 DI | NI **9422** `cDAQ2Mod2/port0/line6` | IFW 15 NO 접점 |
| 점화 SSR | NI **9477** `cDAQ2Mod3/port0/line3` | SSR 입력 수동 ON/OFF |
| 예비 밸브 DO | NI **9477** `cDAQ2Mod3/port0/line4` | **DO 4** 수동 ON/OFF (DO0~2와 동일 SINK) |
| 여유 DO | NI **9477** `cDAQ2Mod3/port0/line5` | **DO 5** 수동 ON/OFF |
| TC 모니터링(선택) | NI **9214** `cDAQ2Mod4/ai0:9` | K형 CH0~4 / T형 CH5~9 |

## MP5Y 유량 표시 설정 (확정)

- 유량계: **OF05ZAT-AR** (전압 펄스 출력)
- MP5Y 입력방식 **in-A: PnP** ← AR 전압출력은 PnP
- 펄스정수: **0.46 ml/P**
- 모드: **F1 주파수**
- 프리스케일: **2.76 × 10¹ = 27.6**
- 화면 단위: **cc/min** (`표시값 = Hz × 27.6`)

UI는 MP5Y 화면값(cc/min)을 **그대로 PV로 사용**합니다.  
소프트웨어에서 `×0.46×60`을 다시 하지 않습니다. (이중 환산 방지)

## 배선

### 1) 유량계(AR) → MP5Y-25
- 펄스 Out / GND → MP5Y 입력
- MP5Y **in-A = PnP** (전압 입력)

### 2) MP5Y → USB-RS485 → PC
| MP5Y | 컨버터 |
|------|--------|
| A(+) | A |
| B(−) | B |

### 3) NI 9264 AO0 → 물펌프 / 인버터 주파수(V1)

피드백 제어 시작 시 AO0=0~5 V, 중지/안전정지 시 AO0=0 V.
주파수 지령만 쓰는 경우 FX(RUN)는 인버터 측에서 별도 처리합니다. **DO4는 예비 밸브용**이며 피드백이 건드리지 않습니다.

| 신호 | NI 쪽 | 장치 |
|------|--------|------|
| 주파수/지령 0~5 V | 9264 **ao0** | REF.W / 인버터 **V1** |
| AO 공통 | 9264 **AO COM** | 장치 COM |

NG 펌프는 NI 9264 **ao1 (NG PUMP) → NG 펌프 신호 입력**, 해당 채널 **AO COM → NG 펌프 신호 COM**으로 연결합니다. AO0/AO1 COM 공통 가능 여부는 펌프 입력 사양도 확인하십시오.

### 4) 레벨센서 3개 → NI 9422

센서는 Black(+24V COM)을 공유하고 Red/White로 +24V를 출력하는 소싱 배선으로 사용합니다.

| 센서 | Red (HIGH) | White (LOW) | Black (24V COM) |
|------|------------|-------------|-----------------|
| 1 | DI0+ | DI1+ | PSU +24V |
| 2 | DI2+ | DI3+ | PSU +24V |
| 3 | DI4+ | DI5+ | PSU +24V |

- 각 채널의 `DI−`는 PSU 0V에 연결합니다.
- 센서 접점이 붙어 DI+–DI− 사이에 24V가 걸리면 해당 입력이 ON입니다.
- 접점 N/O·N/C 방향은 UI의 HIGH/LOW ON 표시로 현장 확인합니다.

### 5) 전동볼밸브 3개 → NI 9477

NI 9477은 전압을 공급하지 않고 DO를 COM(0V)으로 당기는 **싱킹 출력**입니다.

| 밸브 | 연결 |
|------|------|
| Red | PSU +24V |
| White(SIG) | DO0 / DO1 / DO2 |
| Black(0V) | PSU 0V |
| NI 9477 COM | PSU 0V |

DO가 ON이면 White(SIG)가 0V로 당겨져 밸브에 **열림 명령**을 줍니다.

> 이 상태는 전기적 출력 명령입니다. 3선 밸브에 위치 피드백 접점이 없으므로 실제 기계적 열림/닫힘을 검증할 수는 없습니다.

### 5b) 예비 밸브 DO4 / DO5 → NI 9477

DO0~2와 **동일 SINK 배선**. I/O TEST에서 수동 ON/OFF합니다.

| 신호 | NI 9477 |
|------|---------|
| 예비 밸브 SIG | **DO4** (line4) |
| 여유 DO SIG | **DO5** (line5) |
| COM | PSU 0V |

## 레벨 → 밸브 로직

| 밸브 | LOW 감지 | HIGH 감지 | 둘 다 OFF | 둘 다 ON |
|------|-----------|------------|-----------|-----------|
| 1 급수 | 열림 | 닫힘 | 이전 상태 유지 | 센서 오류·안전 닫힘 |
| 2 배수 | 닫힘 | 열림 | 이전 상태 유지 | 센서 오류·안전 닫힘 |
| 3 배수 | 닫힘 | 열림 | 이전 상태 유지 | 센서 오류·안전 닫힘 |

- NI 9422/9477이 연결되면 별도 시작 버튼 없이 자동 제어가 시작되며, 초기 상태는 모두 닫힘입니다.
- 프로그램 종료 또는 DAQ 오류 시 모두 닫힘 명령입니다. 오류 후에는 DAQ가 실제로 재연결될 때 자동 복귀합니다.

## I/O TEST 팝업

- 메인 화면의 **I/O TEST**에서는 아래를 수동 시험합니다.
  - **AO 2** (0~5 V)
  - **V1~V3 / DO0~2** 전동볼밸브 열기·닫기 (레벨센서 무시)
  - **DO 4 / DO 5** SINK ON/OFF
- **NG PUMP(AO1)**는 메인 UI에서 상시 제어합니다. I/O TEST를 닫아도 NG PUMP 출력은 유지됩니다.
- 물펌프 REF.W(AO0)는 I/O TEST에서 건드리지 않습니다.
- 팝업을 여는 순간 피드백 운전은 정지하고 밸브는 모두 닫힙니다.
- 팝업이 열린 동안 레벨센서 자동 밸브 제어는 일시 정지합니다.
- 팝업을 닫으면 AO2=0 V, DO0~2/DO4/DO5=OFF 후 레벨 자동제어로 복귀합니다.
- 메인 화면과 I/O TEST에서 **DO0~2 기능 이름**을 직접 입력할 수 있고, **밸브 이름 저장**(또는 설정 저장)으로 `settings.json`에 남습니다.
- 화면의 **물 주입시간**은 REF.W 제어가 실제로 켜진 시간만 `시:분:초`로 누적합니다.
- 제어를 껐다가 다시 켜도 이전 시간부터 이어지며, 새 작업은 옆의 **초기화** 버튼으로 `00:00:00`부터 시작합니다. 운전 중 초기화도 가능합니다.

## 통신 장애 시 동작

- **NI-DAQ 미연결:** AO/DO 제어와 피드백 운전을 시작할 수 없습니다.
- EXE는 Python `nidaqmx`/`numpy`를 포함하지 않고 **NI-DAQmx Runtime DLL(`nicaiu`)** 을 직접 호출합니다. 용량이 작고 실행이 빠릅니다. 산업용 PC에는 Runtime만 설치되어 있으면 됩니다.
- NI MAX에서 장치가 보여도 앱이 끊기면 **장치명 불일치**일 수 있습니다. 앱은 9264/9422/9477 모듈 종류로 자동 매칭합니다.
- **MP5Y/COM3 미연결:** 유량 PV가 필요한 피드백 운전만 정지합니다.
- MP5Y가 끊겨도 NI-DAQ이 정상이면 레벨센서 자동 밸브 제어, 점화 DO, 메인 NG PUMP(AO1), I/O TEST의 AO2·DO4·DO5는 계속 사용할 수 있습니다.
- 상태 표시는 `COM3 미연결`, `COM3 연결 · 응답 없음`, `통신 ON`을 구분합니다. COM3 포트가 보인다는 사실만으로 MP5Y Modbus 응답까지 정상이라고 판단하지 않습니다.

## TC MONITORING (선택 NI 9214)

- NI 9214를 사용하지 않을 때는 연결하지 않아도 기존 AO/DI/DO와 MP5Y 제어가 정상 동작합니다.
- 기본 장치명은 `cDAQ2Mod4`이며, TC 창에서 K/T 채널 문자열을 바꿀 수 있습니다.
- CH0~4는 K TYPE, CH5~9는 T TYPE으로 읽습니다.
- DAQmx의 NI 9214 **내장 CJC(냉접점 보상)**를 적용해 °C로 읽습니다.
- 각 행의 `이름`칸에 측정 위치나 용도를 직접 입력할 수 있습니다.
- `이름 저장`을 누르면 채널 이름이 `settings.json`에 저장됩니다.
- 모듈 탈착 또는 TC 읽기 오류는 기존 펌프·밸브 제어를 정지시키지 않습니다.

## VS Code 설치 및 소스 실행 (Windows)

1. Microsoft 공식 사이트에서 **Visual Studio Code**를 설치합니다: <https://code.visualstudio.com/>
2. Python 공식 사이트에서 **Python 3 (64-bit)**를 설치하고, 설치 화면에서 **Add Python to PATH**를 선택합니다: <https://www.python.org/downloads/windows/>
3. VS Code를 열고 Extensions에서 Microsoft의 **Python** 확장을 설치합니다.
4. 프로젝트 ZIP을 압축 해제한 뒤 VS Code에서 `File → Open Folder`로 폴더를 엽니다.
5. VS Code의 `Terminal → New Terminal`에서 실행합니다.

```powershell
py -m pip install -r requirements.txt
py pump.py
```

NI 하드웨어를 사용하는 PC에는 별도로 **NI-DAQmx Runtime**을 설치하고, NI MAX 장치명을 `cDAQ2`, `cDAQ2Mod1`, `cDAQ2Mod2`, `cDAQ2Mod3`로 맞춥니다.

## 산업용 PC용 EXE 및 ZIP 만들기

개발 PC의 프로젝트 폴더에서 `build_exe.bat`를 더블클릭하거나 PowerShell에서 실행합니다.

```powershell
.\build_exe.bat
```

완료 후 자동으로 다음 파일이 생성됩니다.

- `dist\PulseFlow.exe`: 실행 파일
- `deploy\`: 산업용 PC 복사용 폴더
- `PulseFlow_deploy.zip`: 압축 배포 파일

`PulseFlow_deploy.zip`을 USB로 산업용 PC에 복사해 압축 해제하고 `PulseFlow.exe`를 실행합니다. 산업용 PC에도 **NI-DAQmx Runtime**은 별도 설치해야 하며, MP5Y USB-RS485 COM 포트도 설정 화면에서 확인합니다.

소스 자체를 압축하려면 프로젝트 폴더의 상위 폴더에서 다음을 실행합니다.

```powershell
Compress-Archive -Path .\DD\* -DestinationPath .\DD_source.zip -Force
```

## 아이콘 바꾸기

창/작업표시줄·exe 아이콘 파일:

- `flow_ui/assets/app_icon.ico`  (Windows / exe)
- `flow_ui/assets/app_icon.png`  (창 아이콘 보조)

원하는 이미지로 위 두 파일을 덮어쓰면 됩니다.  
exe까지 아이콘을 넣으려면 프로젝트 루트에서 `build_exe.bat` 실행 → `dist/PulseFlow.exe`

## 화면
화면 크기를 감지해 자동으로 레이아웃이 바뀝니다.

### 7인치 터치 모드 (`1024×600` 권장)

- 펌프 / 레벨·밸브 / 화염·점화를 큰 터치 탭으로 분리
- 데스크톱 SV 입력은 값을 바꾼 뒤 **Enter**를 눌러야 제어에 적용
- 터치 화면은 SV 숫자를 누르면 키패드가 열리고 **적용** 버튼으로 확정
- SV `-10 / -1 / +1 / +10` 조절
- `50 / 100 / 120 / 150 cc/min` 빠른 프리셋
- P/I/LPF/펄스정수와 I/O TEST 전압도 화면 키패드로 입력

Windows 디스플레이 해상도는 모니터 기본값인 `1024×600`, 배율은 우선 `100%`로 사용하세요.

### 일반 모니터

1. 좌측 상단 펌프 제어 — PV/SV 한 줄 표시, P/I/LPF, 소형 그래프
2. 우측 상단 화염/점화 — 상태 램프와 소형 수동 SSR 버튼
3. 하단 레벨/밸브 — DAQ 연결 즉시 자동 운전, HIGH/LOW 입력과 밸브 명령
4. `I/O TEST`(AO2/DO5), `TC MON` — 별도 팝업

```text
AO = clamp(P × (SV − PV) + I × ∫(SV − PV) dt, 0, 5 V)
```
