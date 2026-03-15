# 파라미터 시나리오 비교

여러 설정 조합을 **한 번에** 백테스트하고 결과를 표로 비교할 수 있습니다.

## 실행 방법

```bash
# 기존(당일봉 체결) vs 현재(다음봉 시가) 두 가지만 비교
python run_backtest.py --compare-original --years 5

# 기본 시나리오 11개 비교 (run_backtest.py 내장)
python run_backtest.py --compare-scenarios --years 5

# JSON 파일로 시나리오 지정 (추가/수정 편함)
python run_backtest.py --compare-scenarios --scenarios-file scenarios/example_scenarios.json --years 5

# Python 파일로 시나리오 지정 (config 전체 키 사용 가능)
python run_backtest.py --compare-scenarios --scenarios-file scenarios/example_scenarios.py --years 5
```

## 시나리오 파일 형식

### JSON (`example_scenarios.json`)

- 배열로 시나리오 목록
- 각 항목: `"label"`(이름) + config 키=값
- 예: `"FILL_ON_NEXT_BAR_OPEN": true`, `"TP_RR_RATIO": 2.5`

### Python (`example_scenarios.py`)

- `SCENARIOS` 리스트를 정의
- 각 항목: `{"label": "이름", "CONFIG_KEY": value, ...}`
- config에 있는 모든 키 사용 가능 (불리언, 숫자, 문자열)

## 출력 항목

| 항목 | 설명 |
|------|------|
| 최종자산 | 백테스트 종료 시 자산 |
| 수익률 | (최종/초기 - 1) × 100% |
| 최대낙폭 | 구간 내 최대 낙폭(%) |
| 샤프/소르티노/칼마 | 연율화 리스크 지표 |
| 진입/손절/1차TP | 해당 이벤트 횟수 |

마지막에 **최종자산 최고** 시나리오와 **칼마(리스크조정) 최고** 시나리오가 안내됩니다.
