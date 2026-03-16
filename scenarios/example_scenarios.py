# -*- coding: utf-8 -*-
"""
파라미터 시나리오 비교용 예시.
사용: python run_backtest.py --compare-scenarios --scenarios-file scenarios/example_scenarios.py
"""
# 각 항목: label(표시명) + config 키=값. label 제외한 키만 config에 적용됩니다.
# 수석 트레이더 관점에서:
# 1) 나쁜 구간(하락장/고변동)에서 진입 줄이기
# 2) 진입 품질(장악형/눌림) 강화
# 3) 포지션 관리(TP/연말확정) 미세 조정
SCENARIOS = [
    # --- 기준선: 현재 설정 ---
    {
        "label": "기준선: 현재 설정 (6x)",
    },

    # --- 1) 하락장·고변동 필터 강화 ---
    {
        "label": "하락장 필터 강화 (strict_long 강화)",
        "BEAR_MARKET_STRICT_LONG_FILTER": True,
        "BEAR_MARKET_STRICT_LONG_DAYS": 7,
        "BEAR_MARKET_STRICT_LONG_PCT": 0.015,
    },
    {
        "label": "변동성 필터 ON (스킵 92/축소 85)",
        "VOLATILITY_FILTER_ENABLED": True,
        "VOL_ATR_PERCENTILE_SKIP": 92.0,
        "VOL_ATR_PERCENTILE_REDUCE": 85.0,
    },
    {
        "label": "변동성 필터 강하게 (스킵 95/축소 90)",
        "VOLATILITY_FILTER_ENABLED": True,
        "VOL_ATR_PERCENTILE_SKIP": 95.0,
        "VOL_ATR_PERCENTILE_REDUCE": 90.0,
    },
    # 연구용: 매우 약한 변동성 필터 (극단적 구간에서만 진입 억제/축소)
    {
        "label": "연구용: 약한 변동성 필터 (스킵 98/축소 90)",
        "VOLATILITY_FILTER_ENABLED": True,
        "VOL_ATR_PERCENTILE_SKIP": 98.0,
        "VOL_ATR_PERCENTILE_REDUCE": 90.0,
    },
    {
        "label": "하락장+변동성 필터 조합",
        "BEAR_MARKET_STRICT_LONG_FILTER": True,
        "BEAR_MARKET_STRICT_LONG_DAYS": 7,
        "BEAR_MARKET_STRICT_LONG_PCT": 0.015,
        "VOLATILITY_FILTER_ENABLED": True,
        "VOL_ATR_PERCENTILE_SKIP": 92.0,
        "VOL_ATR_PERCENTILE_REDUCE": 85.0,
    },

    # --- 2) 진입 품질 강화: 장악형/눌림 조건 ---
    {
        "label": "장악형 품질 강화 (몸통 ≥1.35배)",
        "ENGULF_BODY_RATIO_MIN": 1.35,
    },
    {
        "label": "더 강한 장악형 (몸통 ≥1.50배)",
        "ENGULF_BODY_RATIO_MIN": 1.5,
    },
    {
        "label": "눌림 더 깊게 (30/55%)",
        "PULLBACK_20": 0.30,
        "PULLBACK_50": 0.55,
    },
    {
        "label": "장악형+눌림 동시 강화",
        "ENGULF_BODY_RATIO_MIN": 1.35,
        "PULLBACK_20": 0.30,
        "PULLBACK_50": 0.55,
    },

    # --- 3) 포지션 관리 (TP/EOY) 변화 ---
    {
        "label": "1차 익절 40% (잔여 60% 더 오래 보유)",
        "TP_FIRST_HALF": 0.4,
    },
    {
        "label": "손익비 2.5 (더 자주 TP)",
        "TP_RR_RATIO": 2.5,
    },
    {
        "label": "손익비 2.5 + 1차 익절 40%",
        "TP_RR_RATIO": 2.5,
        "TP_FIRST_HALF": 0.4,
    },
    {
        "label": "반대 장악형 익절 OFF (연말 확정 위주)",
        "EXIT_ON_OPPOSITE_ENGULF": False,
    },
]
