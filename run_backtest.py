# -*- coding: utf-8 -*-
"""이더리움 1시간봉 백테스트 실행 (5년/10년 선택 가능). 미래 참조 없이 봉 단위 순차 처리."""
import argparse
import itertools
import json
import logging
import re
import sys
from pathlib import Path

import config
from data_fetcher import load_or_fetch_1h
from backtest import run_backtest, print_backtest_summary, _risk_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 시나리오 비교용 기본 조합 (--compare-scenarios). label + config 키만 넣고, label는 표시용으로만 사용.
DEFAULT_SCENARIOS = [
    {"label": "기본 (다음봉시가+변동성필터끔)", "FILL_ON_NEXT_BAR_OPEN": True, "VOLATILITY_FILTER_ENABLED": False},
    {"label": "당일봉 체결 (다음봉시가 끔)", "FILL_ON_NEXT_BAR_OPEN": False, "VOLATILITY_FILTER_ENABLED": False},
    {"label": "변동성필터 켬 (스킵92%·축소85%)", "FILL_ON_NEXT_BAR_OPEN": True, "VOLATILITY_FILTER_ENABLED": True},
    {"label": "변동성필터 켬 + 당일봉 체결", "FILL_ON_NEXT_BAR_OPEN": False, "VOLATILITY_FILTER_ENABLED": True},
    {"label": "변동성 엄격 (스킵95%·축소90%)", "FILL_ON_NEXT_BAR_OPEN": True, "VOLATILITY_FILTER_ENABLED": True, "VOL_ATR_PERCENTILE_SKIP": 95.0, "VOL_ATR_PERCENTILE_REDUCE": 90.0},
    {"label": "다음봉 슬리피지 0 bps", "FILL_ON_NEXT_BAR_OPEN": True, "FILL_NEXT_BAR_SLIPPAGE_BPS": 0},
    {"label": "다음봉 슬리피지 10 bps", "FILL_ON_NEXT_BAR_OPEN": True, "FILL_NEXT_BAR_SLIPPAGE_BPS": 10},
    {"label": "손익비 2.5", "TP_RR_RATIO": 2.5},
    {"label": "손익비 3.0 (기본)", "TP_RR_RATIO": 3.0},
    {"label": "1차 익절 40%", "TP_FIRST_HALF": 0.4},
    {"label": "1차 익절 60%", "TP_FIRST_HALF": 0.6},
]


def _run_scenario(df, initial: float, scenario: dict):
    """시나리오 1회 실행 (label 제외한 키로 config 덮어쓴 뒤 백테스트). 반환: 지표 dict."""
    params = {k: v for k, v in scenario.items() if k != "label"}
    if not params:
        # 파라미터 없으면 현재 config 그대로 1회 실행
        trades, equity, final, info, _ = run_backtest(
            df, initial_capital=initial, leverage=config.LEVERAGE, fee_rate=config.FEE_EFFECTIVE
        )
    else:
        backup = {k: getattr(config, k) for k in params if hasattr(config, k)}
        for k, v in params.items():
            setattr(config, k, v)
        try:
            trades, equity, final, info, _ = run_backtest(
                df, initial_capital=initial, leverage=config.LEVERAGE, fee_rate=config.FEE_EFFECTIVE
            )
        finally:
            for k, v in backup.items():
                setattr(config, k, v)
    risk = _risk_metrics(equity, periods_per_year=8760.0)
    n_entries = sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2", "OPEN_ADD"))
    n_stops = sum(1 for t in trades if t.action == "STOP")
    n_tp = sum(1 for t in trades if t.action == "TP_FIRST")
    return {
        "label": scenario.get("label", str(params)),
        "final": final,
        "return_pct": (final / initial - 1) * 100,
        "max_dd_pct": risk["max_drawdown_pct"],
        "sharpe_annual": risk["sharpe_annual"],
        "sortino_annual": risk["sortino_annual"],
        "calmar_annual": risk["calmar_annual"],
        "n_trades": len(trades),
        "n_entries": n_entries,
        "n_stops": n_stops,
        "n_tp": n_tp,
    }


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
            "n_entries": sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2", "OPEN_ADD")),
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
        "--compare-4h-wick-addon",
        action="store_true",
        help="기존 전략 vs 4h윗꼬리 익절 vs 추가진입(7%) vs 둘 다 적용 — 4가지 비교 후 결과 표 출력",
    )
    parser.add_argument(
        "--apply-best",
        action="store_true",
        help="--optimize-entry-tp와 함께 사용: 베스트 조합을 config.py에 자동 반영",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=5,
        choices=[5, 10],
        metavar="N",
        help="백테스트 기간(년). 10년 선택 시 데이터 수집·연산 시간이 더 걸림 (기본: 5)",
    )
    parser.add_argument(
        "--compare-scenarios",
        action="store_true",
        help="여러 파라미터 시나리오를 한 번에 실행 후 비교 표 출력 (기본 시나리오 또는 --scenarios-file 사용)",
    )
    parser.add_argument(
        "--scenarios-file",
        type=str,
        default=None,
        metavar="PATH",
        help="시나리오 목록 파일 경로 (.py: SCENARIOS 리스트, .json: [{label, ...params}, ...]). --compare-scenarios와 함께 사용.",
    )
    parser.add_argument(
        "--compare-original",
        action="store_true",
        help="기존(당일봉 체결) vs 현재(다음봉 시가) 두 가지만 비교해 표로 출력",
    )
    args = parser.parse_args()

    logger.info("%d년 ETH 1h 데이터 로드 중...", args.years)
    df = load_or_fetch_1h(years=args.years, force_refresh=False)
    if df is None or len(df) < 100:
        logger.error("데이터 부족. .env에 바이낸스 API 키를 설정한 뒤 다시 시도하세요.")
        sys.exit(1)

    initial = 100.0

    if args.compare_original:
        # 기존(당일봉 체결) vs 현재(다음봉 시가) 단두 시나리오 비교
        ORIGINAL_VS_CURRENT = [
            {"label": "기존 (당일봉 체결)", "FILL_ON_NEXT_BAR_OPEN": False, "VOLATILITY_FILTER_ENABLED": False},
            {"label": "현재 (다음봉 시가 + 슬리피지 5bps)", "FILL_ON_NEXT_BAR_OPEN": True, "FILL_NEXT_BAR_SLIPPAGE_BPS": 5, "VOLATILITY_FILTER_ENABLED": False},
        ]
        logger.info("기존 vs 현재 비교 실행 중...")
        results = [_run_scenario(df, initial, s) for s in ORIGINAL_VS_CURRENT]
        print("\n" + "=" * 100)
        print("=== 기존 vs 현재 설정 비교 ===")
        print("=" * 100)
        print(f"{'구분':<40} {'최종자산':>10} {'수익률':>10} {'최대낙폭':>10} {'샤프':>6} {'소르티노':>6} {'칼마':>6} {'진입':>6} {'손절':>6} {'1차TP':>5}")
        print("-" * 100)
        for r in results:
            print(
                f"{r['label']:<40} {r['final']:>10,.0f} {r['return_pct']:>9.1f}% {r['max_dd_pct']:>9.1f}% "
                f"{r['sharpe_annual']:>6.2f} {r['sortino_annual']:>6.2f} {r['calmar_annual']:>6.2f} "
                f"{r['n_entries']:>6} {r['n_stops']:>6} {r['n_tp']:>5}"
            )
        print("-" * 100)
        diff_final = results[1]["final"] - results[0]["final"]
        diff_ret = results[1]["return_pct"] - results[0]["return_pct"]
        print(f"  차이 (현재 - 기존): 최종자산 {diff_final:+,.0f} USDT, 수익률 {diff_ret:+.1f}%p")
        print("  ※ 기존 = 신호 봉 당일 체결(낙관적), 현재 = 다음 봉 시가 체결(실전 근사). 실전 평가는 '현재' 기준 권장.")
        return 0

    if args.compare_scenarios:
        if args.scenarios_file:
            path = Path(args.scenarios_file)
            if not path.exists():
                logger.error("시나리오 파일 없음: %s", path)
                return 1
            if path.suffix.lower() == ".json":
                with open(path, "r", encoding="utf-8") as f:
                    scenarios = json.load(f)
            else:
                # .py: 파일 내 SCENARIOS 리스트 사용
                import importlib.util
                spec = importlib.util.spec_from_file_location("scenarios_module", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                scenarios = getattr(mod, "SCENARIOS", None)
                if scenarios is None:
                    logger.error("시나리오 파일에 SCENARIOS 리스트가 없습니다: %s", path)
                    return 1
        else:
            scenarios = DEFAULT_SCENARIOS
        logger.info("시나리오 %d개 비교 실행 중...", len(scenarios))
        results = []
        for idx, scenario in enumerate(scenarios, 1):
            label = scenario.get("label", f"시나리오{idx}")
            logger.info("  [%d/%d] %s", idx, len(scenarios), label[:50])
            r = _run_scenario(df, initial, scenario)
            r["label"] = label
            results.append(r)
        # 비교 표 출력
        print("\n" + "=" * 120)
        print("=== 파라미터 시나리오 비교 (한 번에 테스트) ===")
        print("=" * 120)
        fmt = (
            "{label:<42} {final:>10,.0f} {return_pct:>8.1f}% {max_dd_pct:>8.1f}% "
            "{sharpe_annual:>6.2f} {sortino_annual:>6.2f} {calmar_annual:>6.2f} {n_entries:>6} {n_stops:>6} {n_tp:>5}"
        )
        print(f"{'시나리오':<42} {'최종자산':>10} {'수익률':>8} {'최대낙폭':>8} {'샤프':>6} {'소르티노':>6} {'칼마':>6} {'진입':>6} {'손절':>6} {'1차TP':>5}")
        print("-" * 120)
        for r in results:
            print(
                fmt.format(
                    label=(r["label"][:40] + "..") if len(r["label"]) > 42 else r["label"],
                    final=r["final"],
                    return_pct=r["return_pct"],
                    max_dd_pct=r["max_dd_pct"],
                    sharpe_annual=r["sharpe_annual"],
                    sortino_annual=r["sortino_annual"],
                    calmar_annual=r["calmar_annual"],
                    n_entries=r["n_entries"],
                    n_stops=r["n_stops"],
                    n_tp=r["n_tp"],
                )
            )
        best_return = max(results, key=lambda x: x["final"])
        best_calmar = max(results, key=lambda x: x["calmar_annual"]) if results else None
        print("-" * 120)
        print(f"  최종자산 최고: {best_return['label']}")
        if best_calmar and best_calmar != best_return:
            print(f"  칼마(리스크조정) 최고: {best_calmar['label']}")
        print("  ※ 시나리오 추가/수정: run_backtest.py의 DEFAULT_SCENARIOS 수정 또는 --scenarios-file 로 .py/.json 파일 지정")
        return 0

    if args.compare_short_entry:
        results = []
        for label, wick_short_on in [("기존만 (장악형+반등)", False), ("기존 + 일봉 윗꼬리+음봉 숏", True)]:
            config.DAILY_WICK_BEAR_SHORT_ENABLED = wick_short_on
            trades, equity, final, info, _ = run_backtest(
                df, initial_capital=initial, leverage=config.LEVERAGE, fee_rate=config.FEE_EFFECTIVE
            )
            ret_pct = (final / initial - 1) * 100
            n_entries = sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2", "OPEN_ADD"))
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

    if args.compare_4h_wick_addon:
        # 기존 vs 4h윗꼬리 익절 vs 추가진입(7%) vs 둘 다 — 비교
        backup_wick = getattr(config, "TP_4H_WICK_EXIT_ENABLED", False)
        backup_addon = getattr(config, "ADD_ON_ENTRY_ENABLED", False)
        scenarios = [
            ("기존 전략 (4h윗꼬리·추가진입 없음)", False, False),
            ("4h 윗꼬리 익절만 (윗꼬리>4×몸통 후 저가대비 20% 반등 시 익절)", True, False),
            ("추가 진입만 (포지션 보유 중 신호 시 7%)", False, True),
            ("4h 윗꼬리 익절 + 추가 진입", True, True),
        ]
        results = []
        try:
            for label, wick_on, addon_on in scenarios:
                config.TP_4H_WICK_EXIT_ENABLED = wick_on
                config.ADD_ON_ENTRY_ENABLED = addon_on
                trades, equity, final, info, _ = run_backtest(
                    df, initial_capital=initial, leverage=config.LEVERAGE, fee_rate=config.FEE_EFFECTIVE
                )
                ret_pct = (final / initial - 1) * 100
                n_entries = sum(1 for t in trades if t.action in ("OPEN_1", "OPEN_2", "OPEN_ADD"))
                n_stops = sum(1 for t in trades if t.action == "STOP")
                n_tp = sum(1 for t in trades if t.action == "TP_FIRST")
                n_tp_4h = sum(1 for t in trades if t.action == "TP_4H_WICK_EXIT")
                n_open_add = sum(1 for t in trades if t.action == "OPEN_ADD")
                results.append({
                    "label": label,
                    "final": final,
                    "ret_pct": ret_pct,
                    "n_entries": n_entries,
                    "n_stops": n_stops,
                    "n_tp": n_tp,
                    "n_tp_4h": n_tp_4h,
                    "n_open_add": n_open_add,
                })
        finally:
            config.TP_4H_WICK_EXIT_ENABLED = backup_wick
            config.ADD_ON_ENTRY_ENABLED = backup_addon
        print("\n=== 4h 윗꼬리 익절 · 추가 진입 비교 ===")
        print(
            f"{'전략':<42} {'최종자산':>12} {'수익률':>10} {'진입':>6} {'추가':>6} {'손절':>6} {'1차TP':>6} {'4h윗꼬리':>8}"
        )
        print("-" * 100)
        for r in results:
            print(
                f"{r['label']:<42} {r['final']:>11,.2f} {r['ret_pct']:>9.2f}% "
                f"{r['n_entries']:>6} {r['n_open_add']:>6} {r['n_stops']:>6} {r['n_tp']:>6} {r['n_tp_4h']:>8}"
            )
        best = max(results, key=lambda x: x["final"])
        print(f"\n  → 최종자산 기준 베스트: {best['label']}")
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
        # 실거래 권장 상한: 백테스트는 청산·슬리피지를 완전 반영하지 않음. 10x 이상은 작은 역방향에 청산될 수 있어, 추천은 이 상한 이내에서만.
        RECOMMENDED_LEVERAGE_CAP = 6

        leverages = list(range(2, 16))
        results = []
        for lev in leverages:
            trades, equity, final, info, _ = run_backtest(
                df,
                initial_capital=initial,
                leverage=lev,
                fee_rate=config.FEE_EFFECTIVE,
            )
            ret_pct = (final / initial - 1) * 100
            risk = _risk_metrics(equity, periods_per_year=8760.0)
            n_liq = sum(1 for t in trades if getattr(t, "action", "") == "LIQUIDATION")
            max_dd = risk["max_drawdown_pct"]
            calmar = ret_pct / (abs(max_dd) + 1e-6) if max_dd != 0 else ret_pct
            results.append({
                "lev": lev,
                "final": final,
                "ret_pct": ret_pct,
                "max_dd_pct": max_dd,
                "sharpe": risk["sharpe_annual"],
                "calmar": calmar,
                "n_liq": n_liq,
            })
        best_return = max(results, key=lambda x: x["final"])
        valid = [r for r in results if r["max_dd_pct"] > -99.9]
        # 실거래 추천: 상한(6x) 이내에서만 Calmar 최대 선택 (고레버/100x는 실전 청산 리스크로 사용 비권장)
        valid_capped = [r for r in valid if r["lev"] <= RECOMMENDED_LEVERAGE_CAP]
        if valid_capped:
            best_risk_adj = max(valid_capped, key=lambda x: (x["calmar"], x["ret_pct"]))
        else:
            best_risk_adj = max(valid, key=lambda x: (x["calmar"], x["ret_pct"])) if valid else best_return

        print("\n=== 레버리지 비교 분석 (2~15x, 청산 시뮬레이션 적용) ===")
        print(f"{'레버':<6} {'최종자산':>12} {'수익률':>10} {'최대낙폭':>10} {'Calmar':>8} {'샤프(연)':>10} {'청산':>6}")
        print("-" * 68)
        for r in results:
            mark_ret = "  ← 최고수익" if r["lev"] == best_return["lev"] else ""
            mark_ra = "  ← 실거래 추천" if r["lev"] == best_risk_adj["lev"] else ""
            print(
                f"{r['lev']:<6} {r['final']:>11,.2f} {r['ret_pct']:>9.2f}% {r['max_dd_pct']:>9.2f}% "
                f"{r['calmar']:>8.2f} {r['sharpe']:>10.2f} {r['n_liq']:>6}{mark_ret}{mark_ra}"
            )
        print(f"\n  ※ 최고수익 = 최종자산 최대. 실거래 추천 = {RECOMMENDED_LEVERAGE_CAP}x 이하 중 Calmar 최대 (고레버·100x는 실전 청산 위험으로 비권장).")
        chosen = best_risk_adj
        if best_return["n_liq"] > 0:
            print(f"  ※ 레버 {best_return['lev']}x에서 청산 {best_return['n_liq']}회 발생 → 실거래 시 고레버 위험.")
        config_path = Path(__file__).resolve().parent / "config.py"
        raw = config_path.read_text(encoding="utf-8")
        if "LEVERAGE =" in raw or "LEVERAGE=" in raw:
            raw = re.sub(r"LEVERAGE\s*=\s*\d+", f"LEVERAGE = {chosen['lev']}", raw, count=1)
            config_path.write_text(raw, encoding="utf-8")
            print(f"\n  → config.py에 LEVERAGE = {chosen['lev']} (실거래 추천, {RECOMMENDED_LEVERAGE_CAP}x 이하) 저장됨.")
        trades, equity, final, info, df_used = run_backtest(
            df, initial_capital=initial, leverage=chosen["lev"], fee_rate=config.FEE_EFFECTIVE
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
