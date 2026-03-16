# -*- coding: utf-8 -*-
"""
연말 확정(PROFIT_LOCK_PERIOD=year) 유지 하에 파라미터 그리드 탐색.
최종 자산이 최대가 되는 조합을 찾고, 선택 시 config.py에 반영합니다.
"""
import itertools
import logging
import sys
from pathlib import Path

import config
from data_fetcher import load_or_fetch_5y_1h
from backtest import run_backtest

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


def run_single(df, initial: float, params: dict) -> dict:
    """주어진 params로 config를 덮어쓴 뒤 백테스트 1회 실행. 원래 config는 복원."""
    backup = {}
    for key, value in params.items():
        if hasattr(config, key):
            backup[key] = getattr(config, key)
            setattr(config, key, value)
    try:
        trades, equity, final, info, _ = run_backtest(
            df,
            initial_capital=initial,
            leverage=config.LEVERAGE,
            fee_rate=config.FEE_EFFECTIVE,
        )
        max_eq = float(equity.max()) if equity is not None and len(equity) else final
        min_eq = float(equity.min()) if equity is not None and len(equity) else final
        n_stops = sum(1 for t in trades if t.action == "STOP")
        n_eoy = sum(1 for t in trades if t.action == "EOY_PROFIT_LOCK")
        n_tp = sum(1 for t in trades if t.action == "TP_FIRST")
        return {
            "final": final,
            "return_pct": (final / initial - 1) * 100,
            "max_equity": max_eq,
            "min_equity": min_eq,
            "n_trades": len(trades),
            "n_stops": n_stops,
            "n_eoy": n_eoy,
            "n_tp": n_tp,
        }
    finally:
        for key in backup:
            setattr(config, key, backup[key])


def main():
    import argparse
    parser = argparse.ArgumentParser(description="연말 확정 하에 파라미터 그리드 최적화")
    parser.add_argument("--apply", action="store_true", help="최적 조합을 config.py에 자동 반영 (확인 없음)")
    parser.add_argument("--quick", action="store_true", help="빠른 탐색: 12개 조합만 (TREND×EXIT×TP_HALF×RR)")
    args = parser.parse_args()

    print("데이터 로드 중...")
    df = load_or_fetch_5y_1h(force_refresh=False)
    if df is None or len(df) < 100:
        print("데이터 부족. .env에 바이낸스 API 키를 설정한 뒤 다시 시도하세요.", file=sys.stderr)
        return 1

    # 연말 확정 고정
    config.EOY_CLOSE_IF_PROFIT = True
    config.PROFIT_LOCK_PERIOD = "year"

    initial = 100.0

    # 그리드: 연말 확정 극대화에 영향 큰 파라미터 (실행 시간 고려해 조합 수 제한)
    if args.quick:
        grid = {
            "TREND_4H_MIN_PCT_ABOVE_EMA": [0.005, 0.01],
            "EXIT_ON_OPPOSITE_ENGULF": [True, False],
            "TP_FIRST_HALF": [0.4, 0.6],
            "TP_RR_RATIO": [2.5, 3.0],
            "ENGULF_BODY_RATIO_MIN": [1.2, 1.35],
        }
    else:
        grid = {
            "TREND_4H_MIN_PCT_ABOVE_EMA": [0.005, 0.01, 0.015],
            "EXIT_ON_OPPOSITE_ENGULF": [True, False],
            "TP_FIRST_HALF": [0.4, 0.5, 0.6],
            "TP_RR_RATIO": [2.5, 3.0],
            "ENGULF_BODY_RATIO_MIN": [1.2, 1.25, 1.3, 1.35],
        }

    keys = list(grid.keys())
    values = list(grid.values())
    combinations = list(itertools.product(*values))
    n_total = len(combinations)
    print(f"총 {n_total}개 조합 탐색 중 (연말 확정=year 고정)...\n")

    results = []
    for idx, combo in enumerate(combinations, 1):
        params = dict(zip(keys, combo))
        r = run_single(df, initial, params)
        r["params"] = params.copy()
        results.append(r)
        print(f"  [{idx}/{n_total}] final={r['final']:.2f} ret={r['return_pct']:.1f}% "
              f"| TREND={params['TREND_4H_MIN_PCT_ABOVE_EMA']} EXIT_OPP={params['EXIT_ON_OPPOSITE_ENGULF']} "
              f"TP_HALF={params['TP_FIRST_HALF']} RR={params['TP_RR_RATIO']} ENGULF={params['ENGULF_BODY_RATIO_MIN']}")

    # 최종 자산 기준 정렬
    results.sort(key=lambda x: x["final"], reverse=True)
    best = results[0]
    best_params = best["params"]

    print("\n" + "=" * 70)
    print("=== 최적 조합 (최종 자산 기준) ===")
    print("=" * 70)
    print(f"최종 자산: {best['final']:.2f} USDT")
    print(f"수익률: {best['return_pct']:.2f}%")
    print(f"최대/최소 자산: {best['max_equity']:.2f} / {best['min_equity']:.2f}")
    print(f"손절/연말확정/1차익절: {best['n_stops']} / {best['n_eoy']} / {best['n_tp']}")
    print("\n권장 설정:")
    for k, v in best_params.items():
        if isinstance(v, bool):
            print(f"  {k} = {v}")
        elif isinstance(v, float):
            print(f"  {k} = {v}")
        else:
            print(f"  {k} = {v}")

    # config.py 반영 여부
    do_apply = args.apply or (input("\n이 설정을 config.py에 적용할까요? (y/n): ").strip().lower() == "y")
    if do_apply:
        config_path = Path(__file__).resolve().parent / "config.py"
        raw = config_path.read_text(encoding="utf-8")
        import re
        for key, value in best_params.items():
            if key not in ("TREND_4H_MIN_PCT_ABOVE_EMA", "EXIT_ON_OPPOSITE_ENGULF",
                           "TP_FIRST_HALF", "TP_RR_RATIO", "ENGULF_BODY_RATIO_MIN"):
                continue
            if isinstance(value, bool):
                pattern = rf"({key}\s*=\s*)(True|False)"
                raw = re.sub(pattern, rf"\g<1>{str(value)}", raw, count=1)
            elif isinstance(value, str):
                pattern = rf'({key}\s*=\s*)"[^"]*"'
                raw = re.sub(pattern, rf'\g<1>"{value}"', raw, count=1)
            elif isinstance(value, (int, float)):
                pattern = rf"({key}\s*=\s*)[\d.]+"
                raw = re.sub(pattern, rf"\g<1>{value}", raw, count=1)
        config_path.write_text(raw, encoding="utf-8")
        print("config.py에 적용했습니다.")
    else:
        print("적용하지 않았습니다. 위 설정을 수동으로 config.py에 넣어주세요.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
