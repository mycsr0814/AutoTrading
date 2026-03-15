# -*- coding: utf-8 -*-
"""5년 이더리움 1시간봉 백테스트 실행. 미래 참조 없이 봉 단위 순차 처리."""
import argparse
import itertools
import logging
import re
import sys
from pathlib import Path

import config
from data_fetcher import load_or_fetch_5y_1h
from backtest import run_backtest, print_backtest_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _run_single_with_params(df, initial: float, params: dict):
    """params로 config 덮어쓴 뒤 백테스트 1회. 원래 config 복원."""
    backup = {k: getattr(config, k) for k in params if hasattr(config, k)}
    for k, v in params.items():
        setattr(config, k, v)
    try:
        trades, equity, final, info, _ = run_backtest(
            df,
            initial_capital=initial,
            leverage=config.LEVERAGE,
            fee_rate=config.FEE_EFFECTIVE,
        )
        n_stops = sum(1 for t in trades if t.action == "STOP")
        n_tp = sum(1 for t in trades if t.action == "TP_FIRST")
        return {
            "final": final,
            "return_pct": (final / initial - 1) * 100,
            "n_stops": n_stops,
            "n_tp": n_tp,
            "n_entries": sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2")),
        }
    finally:
        for k, v in backup.items():
            setattr(config, k, v)


def main():
    parser = argparse.ArgumentParser(description="ETH 1h 백테스트")
    parser.add_argument(
        "--compare-periods",
        action="store_true",
        help="연/월/주/일 수익 확정을 넷 다 테스트한 뒤 최고 수익 주기로 설정하고 백테스트 실행",
    )
    parser.add_argument(
        "--optimize-leverage",
        action="store_true",
        help="레버리지 3~10 각각 백테스트 후 최고 수익률 레버리지 출력 및 config 반영",
    )
    parser.add_argument(
        "--compare-short-entry",
        action="store_true",
        help="숏 진입 방식 비교: 기존(장악형+반등) vs 기존+일봉 윗꼬리저항+음봉(EMA20아래) 숏, 각각 백테스트 후 결과 출력",
    )
    parser.add_argument(
        "--optimize-entry-tp",
        action="store_true",
        help="진입·익절 파라미터 그리드 탐색 후 최종 자산 최대 조합 출력 (선택 시 config 반영)",
    )
    parser.add_argument(
        "--apply-best",
        action="store_true",
        help="--optimize-entry-tp와 함께 사용: 베스트 조합을 config.py에 자동 반영",
    )
    args = parser.parse_args()

    logger.info("5년 ETH 1h 데이터 로드 중...")
    df = load_or_fetch_5y_1h(force_refresh=False)
    if df is None or len(df) < 100:
        logger.error("데이터 부족. .env에 바이낸스 API 키를 설정한 뒤 다시 시도하세요.")
        sys.exit(1)

    initial = 100.0

    if args.compare_short_entry:
        results = []
        for label, wick_short_on in [("기존만 (장악형+반등)", False), ("기존 + 일봉 윗꼬리+음봉 숏", True)]:
            config.DAILY_WICK_BEAR_SHORT_ENABLED = wick_short_on
            trades, equity, final, info, _ = run_backtest(
                df, initial_capital=initial, leverage=config.LEVERAGE, fee_rate=config.FEE_EFFECTIVE
            )
            ret_pct = (final / initial - 1) * 100
            n_entries = sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2"))
            results.append((label, final, ret_pct, n_entries))
        print("\n=== 숏 진입 방식 비교 ===")
        print(f"{'방식':<32} {'최종자산':>12} {'수익률':>10} {'진입수':>8}")
        print("-" * 66)
        for label, final_cap, ret, n_ent in results:
            print(f"{label:<32} {final_cap:>11,.2f} {ret:>9.2f}% {n_ent:>8}")
        best = max(results, key=lambda x: x[1])
        print(f"\n  → 최종자산 기준 베스트: {best[0]}")
        config.DAILY_WICK_BEAR_SHORT_ENABLED = best[0] == "기존 + 일봉 윗꼬리+음봉 숏"
        return 0

    if args.optimize_entry_tp:
        # 진입·익절 그리드: 1차/2차 눌림, 진입 비중, 손익비, 1차 익절 비율 (64조합)
        grid = {
            "PULLBACK_20": [0.20, 0.25],
            "PULLBACK_50": [0.50, 0.55],
            "FIRST_ENTRY_PCT": [0.10, 0.12],
            "SECOND_ENTRY_PCT": [0.30, 0.32],
            "TP_RR_RATIO": [2.5, 3.0],
            "TP_FIRST_HALF": [0.4, 0.5, 0.6],
        }
        keys = list(grid.keys())
        combos = list(itertools.product(*(grid[k] for k in keys)))
        n_total = len(combos)
        logger.info("진입·익절 그리드 %d개 조합 탐색 중...", n_total)
        results = []
        for idx, combo in enumerate(combos, 1):
            params = dict(zip(keys, combo))
            r = _run_single_with_params(df, initial, params)
            r["params"] = params
            results.append(r)
            if idx % 24 == 0 or idx == n_total:
                logger.info("  [%d/%d] 완료 (현재 베스트: %.2f)", idx, n_total, max(x["final"] for x in results))
        results.sort(key=lambda x: x["final"], reverse=True)
        best = results[0]
        bp = best["params"]
        print("\n=== 진입·익절 최적화 결과 (최종 자산 기준) ===")
        print(f"베스트 최종 자산: {best['final']:.2f} USDT  |  수익률: {best['return_pct']:.2f}%")
        print(f"손절 횟수: {best['n_stops']}  |  1차 익절: {best['n_tp']}  |  진입 횟수: {best['n_entries']}")
        print("\n베스트 파라미터:")
        for k in keys:
            print(f"  {k} = {bp[k]}")
        print("\n상위 5개 조합:")
        print(f"{'PULL_20':<8} {'PULL_50':<8} {'ENT1':<6} {'ENT2':<6} {'RR':<5} {'TP_HALF':<8} {'최종자산':>12} {'수익률':>10}")
        print("-" * 72)
        for r in results[:5]:
            p = r["params"]
            print(
                f"{p['PULLBACK_20']:<8.2f} {p['PULLBACK_50']:<8.2f} "
                f"{p['FIRST_ENTRY_PCT']:<6.2f} {p['SECOND_ENTRY_PCT']:<6.2f} "
                f"{p['TP_RR_RATIO']:<5.1f} {p['TP_FIRST_HALF']:<8.1f} "
                f"{r['final']:>11,.2f} {r['return_pct']:>9.2f}%"
            )
        if args.apply_best:
            config_path = Path(__file__).resolve().parent / "config.py"
            raw = config_path.read_text(encoding="utf-8")
            for key in keys:
                val = bp[key]
                if isinstance(val, float):
                    pattern = rf"({re.escape(key)}\s*=\s*)[\d.]+"
                    raw = re.sub(pattern, rf"\g<1>{val}", raw, count=1)
            config_path.write_text(raw, encoding="utf-8")
            print("\n  → config.py에 베스트 파라미터 적용 완료.")
        else:
            print("\n  → 적용하려면: python run_backtest.py --optimize-entry-tp --apply-best")
        return 0

    if args.optimize_leverage:
        leverages = list(range(3, 11))
        results = []
        for lev in leverages:
            trades, equity, final, info, _ = run_backtest(
                df,
                initial_capital=initial,
                leverage=lev,
                fee_rate=config.FEE_EFFECTIVE,
            )
            ret_pct = (final / initial - 1) * 100
            results.append((lev, final, ret_pct))
        best = max(results, key=lambda x: x[1])
        print("\n=== 레버리지 최적화 (3~10) ===")
        print(f"{'레버리지':<8} {'최종자산':>12} {'수익률':>10}")
        print("-" * 34)
        for lev, final_cap, ret in results:
            mark = "  ← 베스트" if lev == best[0] else ""
            print(f"{lev:<8} {final_cap:>11,.2f} {ret:>9.2f}%{mark}")
        config_path = Path(__file__).resolve().parent / "config.py"
        raw = config_path.read_text(encoding="utf-8")
        if "LEVERAGE =" in raw or "LEVERAGE=" in raw:
            raw = re.sub(r"LEVERAGE\s*=\s*\d+", f"LEVERAGE = {best[0]}", raw, count=1)
            config_path.write_text(raw, encoding="utf-8")
            print(f"\n  → config.py에 LEVERAGE = {best[0]} 로 저장됨.")
        trades, equity, final, info, df_used = run_backtest(
            df, initial_capital=initial, leverage=best[0], fee_rate=config.FEE_EFFECTIVE
        )
        print_backtest_summary(trades, equity, final, initial, info=info, df=df_used)
        return 0

    if args.compare_periods:
        # 연·월·주 각각 테스트 후 베스트 주기로 설정
        if not getattr(config, "EOY_CLOSE_IF_PROFIT", False):
            config.EOY_CLOSE_IF_PROFIT = True
        results = []
        for period in ("year", "month", "week", "day"):
            config.PROFIT_LOCK_PERIOD = period
            trades, equity, final, info, df_used = run_backtest(
                df,
                initial_capital=initial,
                leverage=config.LEVERAGE,
                fee_rate=config.FEE_EFFECTIVE,
            )
            ret_pct = (final / initial - 1) * 100
            results.append((period, final, ret_pct))
        # 베스트 = 최종 자산 최대
        best = max(results, key=lambda x: x[1])
        config.PROFIT_LOCK_PERIOD = best[0]
        print("\n=== 주기별 수익 확정 비교 (연/월/주/일) ===")
        print(f"{'주기':<8} {'최종자산':>12} {'수익률':>10}")
        print("-" * 34)
        for period, final_cap, ret in results:
            mark = "  ← 베스트" if period == best[0] else ""
            print(f"{period:<8} {final_cap:>11,.2f} {ret:>9.2f}%{mark}")
        # config.py에 베스트 주기 반영
        config_path = Path(__file__).resolve().parent / "config.py"
        raw = config_path.read_text(encoding="utf-8")
        if 'PROFIT_LOCK_PERIOD = "' in raw or "PROFIT_LOCK_PERIOD = '" in raw:
            raw = re.sub(
                r'PROFIT_LOCK_PERIOD\s*=\s*["\'](?:year|month|week|day)["\']',
                f'PROFIT_LOCK_PERIOD = "{best[0]}"',
                raw,
                count=1,
            )
            config_path.write_text(raw, encoding="utf-8")
            print(f"  → config.py에 PROFIT_LOCK_PERIOD = \"{best[0]}\" 로 저장됨.\n")
        else:
            print()
        logger.info("백테스트 실행 (미래 참조 없음, 수수료·슬리피지 반영)... [주기=%s]", best[0])
        trades, equity, final, info, df_used = run_backtest(
            df,
            initial_capital=initial,
            leverage=config.LEVERAGE,
            fee_rate=config.FEE_EFFECTIVE,
        )
        print_backtest_summary(trades, equity, final, initial, info=info, df=df_used)
        return 0

    logger.info("백테스트 실행 (미래 참조 없음, 수수료·슬리피지 반영)...")
    trades, equity, final, info, df_used = run_backtest(
        df,
        initial_capital=initial,
        leverage=config.LEVERAGE,
        fee_rate=config.FEE_EFFECTIVE,
    )
    print_backtest_summary(trades, equity, final, initial, info=info, df=df_used)
    return 0


if __name__ == "__main__":
    sys.exit(main())
