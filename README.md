# MFC 유량 환산

MFC 설정값과 유량계 실측값이 어긋나서, 그래프 실측으로 환산식을 다시 맞췄습니다.

## 실측 (LPM)

| MFC 설정 | 유량계 실측 |
| --- | --- |
| 30 | 38 |
| 40 | 49 |
| 50 | 61 |

저유량 참고: 설정 5 → 실측 5.5, 설정 10 → 실측 9.8 (선형식 범위 밖 참고값)

## 환산식

- **현재:** `실측 y = 1.15 × 설정 x + 3.3333` (그래프 추세선, R² ≈ 0.9994)
- **이전(폐기):** `y = 0.9337x + 4.6452` — 위 실측과 맞지 않음

보정/보상:

- 실측 → 보정값: `x = (y − 3.3333) / 1.15`
- 목표 실측을 만들 설정: `x = (목표 − 3.3333) / 1.15`

예: 실측 30 LPM이 필요하면 MFC에는 약 **23.19** 를 넣습니다.

## 사용

```bash
python -m mfc_calibration.cli table
python -m mfc_calibration.cli expected 30      # 설정 → 예상 실측
python -m mfc_calibration.cli correct 38       # 실측 → 보정 환산값
python -m mfc_calibration.cli compensate 30    # 목표 실측 → 넣을 설정
python -m unittest mfc_calibration.test_conversion
```

코드에서:

```python
from mfc_calibration import compensated_setpoint, corrected_from_measured, expected_measured

expected_measured(30)       # 37.8333
corrected_from_measured(38) # 30.145...
compensated_setpoint(30)    # 23.188...
```

DAQ 채널/UI와는 별개입니다. MFC 설정·실측 유량 환산만 다룹니다.
