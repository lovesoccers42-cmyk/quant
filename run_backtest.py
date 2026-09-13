# -*- coding: utf-8 -*-
"""백테스트 실행 → 엑셀 + JSON 출력.

    python run_backtest.py kr 30 ME
    python run_backtest.py us 50 QE
"""
import json
import logging
import sys
from datetime import date

import pandas as pd

import backtest
import config

log = logging.getLogger("quant_agent.backtest")


def _summary_rows(s: dict) -> pd.DataFrame:
    rows = []
    for k, v in s.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                rows.append({"구분": k, "항목": kk, "값": vv})
        elif isinstance(v, list):
            for item in v:
                rows.append({"구분": k, "항목": "", "값": item})
        else:
            rows.append({"구분": k, "항목": "", "값": v})
    return pd.DataFrame(rows)


def main(market: str = "kr", top_n: int = 30, rebalance: str = "ME",
         cost_bps: float = 25.0, max_sector_pct: float = -1.0,
         min_turnover: float = -1.0) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"backtest_{market}_{date.today():%Y%m%d}.log",
            encoding="utf-8"), logging.StreamHandler()],
        force=True)

    # 음수면 config 기본값. 0(섹터상한 1.0)이면 제약 없이 = 예전 동작
    if max_sector_pct < 0:
        max_sector_pct = config.MAX_SECTOR_PCT
    if min_turnover < 0:
        min_turnover = (config.MIN_TURNOVER_KR if market == "kr"
                        else config.MIN_TURNOVER_US)

    res = backtest.run(market=market, top_n=top_n, rebalance=rebalance,
                       cost_bps=cost_bps, max_sector_pct=max_sector_pct,
                       min_turnover=min_turnover)
    s = res["summary"]
    perf, holdings = res["perf"], res["holdings"]

    label = backtest.SPECS[market].label
    today = f"{date.today():%Y%m%d}"
    name = f"{config.REPORT_NAME} 백테스트 {label} {today[4:]}_{config.REPORT_SUFFIX}"

    xlsx = config.OUTPUT_DIR / f"{name}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        _summary_rows(s).to_excel(xw, sheet_name="요약", index=False)
        out = perf.copy()
        for c in ("수익률", "비용차감수익률", "벤치마크", "턴오버"):
            out[c] = (out[c] * 100).round(2)
        out.to_excel(xw, sheet_name="리밸런싱", index=False)
        if len(holdings):
            holdings.to_excel(xw, sheet_name="보유종목", index=False)

    curve = {
        "dates": [f"{d:%Y-%m-%d}" for d in perf["다음리밸런싱"]],
        "portfolio": [round(float(v), 4) for v in perf["누적"]],
        "benchmark": [round(float(v), 4) for v in perf["누적_벤치마크"]],
    }
    (config.OUTPUT_DIR / f"backtest_{market}_latest.json").write_text(
        json.dumps({"summary": s, "curve": curve, "generated": today},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    p, b = s["포트폴리오"], s["벤치마크(전종목 동일가중)"]
    print("\n" + "=" * 62)
    print(f"  {label}장 QVM 백테스트 · {s['기간']}")
    print("=" * 62)
    print(f"  리밸런싱 {s['리밸런싱횟수']}회 · 보유 {s['보유종목수']}종목 "
          f"· 평균 턴오버 {s['평균턴오버']}%")
    print(f"  {'':14}{'포트폴리오':>12}{'벤치마크':>12}")
    for k in ("누적수익률", "연환산수익률", "연변동성", "최대낙폭", "승률", "샤프"):
        pv, bv = p.get(k), b.get(k)
        unit = "" if k == "샤프" else "%"
        pv = f"{pv}{unit}" if pv is not None else "-"
        bv = f"{bv}{unit}" if bv is not None else "-"
        print(f"  {k:<14}{pv:>12}{bv:>12}")
    print(f"\n  초과수익률: {s['초과수익률']}%p")
    print("\n  제약:")
    for k, v in s.get("제약", {}).items():
        print(f"   · {k}: {v}")
    print("\n  한계:")
    for lim in s["한계"]:
        print(f"   · {lim}")
    print(f"\n  엑셀: {xlsx.name}")
    return 0


if __name__ == "__main__":
    market = sys.argv[1] if len(sys.argv) > 1 else "kr"
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    reb = sys.argv[3] if len(sys.argv) > 3 else "ME"
    cost = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0
    cap = float(sys.argv[5]) if len(sys.argv) > 5 else -1.0
    turn = float(sys.argv[6]) if len(sys.argv) > 6 else -1.0
    sys.exit(main(market, top_n, reb, cost, cap, turn))
