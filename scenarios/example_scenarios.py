# -*- coding: utf-8 -*-
"""
파라미터 시나리오 비교용 예시.
사용: python run_backtest.py --compare-scenarios --scenarios-file scenarios/example_scenarios.py
"""
# 각 항목: label(표시명) + config 키=값. label 제외한 키만 config에 적용됩니다.
SCENARIOS = [
    {"label": "기본 (다음봉시가)", "FILL_ON_NEXT_BAR_OPEN": True, "VOLATILITY_FILTER_ENABLED": False},
    {"label": "당일봉 체결", "FILL_ON_NEXT_BAR_OPEN": False, "VOLATILITY_FILTER_ENABLED": False},
    {"label": "변동성필터 켬", "FILL_ON_NEXT_BAR_OPEN": True, "VOLATILITY_FILTER_ENABLED": True},
    {"label": "손익비 2.5", "TP_RR_RATIO": 2.5},
    {"label": "손익비 3.0", "TP_RR_RATIO": 3.0},
    {"label": "1차 익절 40%", "TP_FIRST_HALF": 0.4},
    {"label": "1차 익절 60%", "TP_FIRST_HALF": 0.6},
]
