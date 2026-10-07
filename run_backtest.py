# -*- coding: utf-8 -*-
"""백테스트 실행 → 엑셀 + JSON 출력.

    python run_backtest.py kr 30 ME
    python run_backtest.py us 50 QE
    python run_backtest.py kr 100 W-FRI 25 0.25 500000000 4   # 주간 4등분
    python run_backtest.py kr 100 ME 25 0.25 500000000 1 200 0  # 200일선 아래면 현금

비중 방식 비교 (A단계) — 앞의 9개 인자는 같게 두고 뒤의 두 개만 바꿉니다:
    python run_backtest.py kr 100 W-FRI 25 0.25 500000000 4 0 0 equal      # 기준선
    python run_backtest.py kr 100 W-FRI 25 0.25 500000000 4 0 0 mcap  0.08
    python run_backtest.py kr 100 W-FRI 25 0.25 500000000 4 0 0 score 0.03
섹터 상한을 풀려면 5번째 인자를 1.0으로:
    python run_backtest.py kr 100 W-FRI 25 1.0 500000000 4 0 0 mcap 0.08
"""
import json
import logging
import sys
from datetime import date

import pandas as pd

import backtest
import config

log = logging.getLogger("quant_agent.backtest")


def _parse_weights(text: str):
    """'0.25,0.5,0.25' → [0.25, 0.5, 0.25]. 빈 값이면 None(기본값)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        out = [float(x) for x in text.replace(" ", "").split(",") if x]
    except ValueError:
        raise ValueError(f"팩터 가중치를 못 읽었습니다: {text!r}. "
                         f"'퀄리티,밸류,모멘텀' 형식으로 주세요 (예 0.25,0.5,0.25)")
    if len(out) not in (3, 4) or sum(out) <= 0:
        raise ValueError(f"팩터 가중치는 3개(QVM) 또는 4개(QVML)여야 합니다: {out}")
    return out


def _parse_split(text: str):
    """'2/1' → 2등분 중 2번째. 빈 값이나 '0'이면 분할 없음."""
    text = (text or "").strip()
    if not text or text in ("0", "1", "none", "없음"):
        return None
    shared = None
    if "@" in text:
        text, sh = text.split("@", 1)
        shared = float(sh)          # 공용 비중 (0.7 = 70%는 두 계좌 공통)
    try:
        mod, rem = text.split("/")
        out = {"mod": int(mod), "rem": int(rem)}
    except Exception:
        raise ValueError(f"교차분할을 못 읽었습니다: {text!r}. "
                         f"'등분수/몇번째' 또는 '등분수/몇번째@공용비중' — "
                         f"예 2/0, 2/1, 2/0@0.7")
    if shared is not None:
        if not 0.0 <= shared <= 1.0:
            raise ValueError(f"공용 비중은 0~1 사이여야 합니다: {shared}")
        out["shared"] = shared
    return out


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


def main(market: str = "kr", top_n: int = 0, rebalance: str = "",
         cost_bps: float = -1.0, max_sector_pct: float = -1.0,
         min_turnover: float = -1.0, tranches: int = 1,
         trend_ma: int = 0, risk_off: float = 0.0,
         weighting: str = "", max_weight: float = -1.0,
         rebal_band: float = -1.0, rank_buffer: float = -1.0,
         sector_neutral: str = "", use_lowvol: int = -1,
         trend_stock: int = -1, max_sector_weight: float = -1.0,
         qvm_weights: str = "", split: str = "") -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"backtest_{market}_{date.today():%Y%m%d}.log",
            encoding="utf-8"), logging.StreamHandler()],
        force=True)

    # 확정 설정이 기본값입니다 (한국 CONFIRMED_KR / 미국 CONFIRMED_US).
    # 인자를 주면 그게 이깁니다. 손으로 입력하다 max_weight 칸에 0.25를 넣어
    # 런 하나를 통째로 날린 적이 있어, 기본값을 코드에 고정했습니다.
    CK = (config.CONFIRMED_KR if market == "kr"
          else getattr(config, "CONFIRMED_US", {}) if market == "us" else {})

    def pick(name, given, fallback):
        """인자 → 확정 설정 → config 순서로 고릅니다."""
        if given is not None and not (isinstance(given, (int, float))
                                      and given < 0):
            if not (isinstance(given, str) and not given.strip()):
                return given
        if name in CK:
            return CK[name]
        return fallback

    max_sector_pct = pick("max_sector_pct", max_sector_pct, config.MAX_SECTOR_PCT)
    min_turnover = pick("min_turnover", min_turnover,
                        config.MIN_TURNOVER_KR if market == "kr"
                        else config.MIN_TURNOVER_US)
    weighting = str(pick("weighting", weighting,
                         config.WEIGHTING or "equal")).strip().lower()
    max_weight = pick("max_weight", max_weight, config.MAX_WEIGHT)
    rebal_band = pick("rebal_band", rebal_band, config.REBAL_BAND)
    rank_buffer = pick("rank_buffer", rank_buffer, config.RANK_BUFFER)

    top_n = int(pick("top_n", top_n or None, config.N_PORTFOLIO))
    rebalance = str(pick("rebalance", rebalance, "ME"))
    cost_bps = float(pick("cost_bps", cost_bps, 25.0))
    tranches = int(pick("tranches", tranches if tranches != 1 else None,
                        tranches))
    trend_ma = int(pick("trend_ma", trend_ma if trend_ma else None, trend_ma))
    risk_off = float(pick("risk_off", risk_off if risk_off else None, risk_off))

    res = backtest.run(market=market, top_n=top_n, rebalance=rebalance,
                       cost_bps=cost_bps, max_sector_pct=max_sector_pct,
                       min_turnover=min_turnover, tranches=max(1, int(tranches)),
                       trend_ma=int(trend_ma), risk_off=float(risk_off),
                       cash_rate=config.CASH_RATE,
                       weighting=weighting, max_weight=float(max_weight),
                       rebal_band=float(rebal_band),
                       rank_buffer=float(rank_buffer),
                       sector_neutral=pick("sector_neutral", sector_neutral,
                                           config.SECTOR_NEUTRAL),
                       use_lowvol=bool(pick("use_lowvol", use_lowvol,
                                            config.USE_LOWVOL)),
                       trend_stock=int(pick("trend_stock", trend_stock,
                                            config.TREND_STOCK_MA)),
                       max_sector_weight=float(pick("max_sector_weight",
                                                    max_sector_weight,
                                                    config.MAX_SECTOR_WEIGHT)),
                       qvm_weights=(_parse_weights(qvm_weights)
                                    or CK.get("qvm_weights")),
                       split=_parse_split(split))
    s = res["summary"]
    perf, holdings = res["perf"], res["holdings"]

    label = backtest.SPECS[market].label
    today = f"{date.today():%Y%m%d}"
    name = f"{config.REPORT_NAME} 백테스트 {label} {today[4:]}_{config.REPORT_SUFFIX}"

    xlsx = config.OUTPUT_DIR / f"{name}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        _summary_rows(s).to_excel(xw, sheet_name="요약", index=False)
        out = perf.copy()
        # 엑셀에서는 전부 % 단위로 통일합니다. 예전에는 벤치마크_유동성만
        # 분수로 남아 있어, 엑셀을 다시 읽어 계산할 때 100배 틀렸습니다.
        for c in ("수익률", "비용차감수익률", "벤치마크", "벤치마크_유동성", "벤치마크_지수", "턴오버",
                  "최대종목비중", "상위10비중",
                  "최대섹터비중", "섹터상한깎음"):
            if c in out.columns:
                out[c] = (out[c] * 100).round(3)
        out.to_excel(xw, sheet_name="리밸런싱", index=False)
        if len(holdings):
            holdings.to_excel(xw, sheet_name="보유종목", index=False)
        if len(res.get("yearly", [])):
            res["yearly"].to_excel(xw, sheet_name="연도별", index=False)
        if len(res.get("ic", [])):
            res["ic"].to_excel(xw, sheet_name="신호강도", index=False)

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
          f"· 회차당 턴오버 {s['평균턴오버']}% (연환산 {s['연환산턴오버']:.0f}%)")
    print(f"  {'':14}{'포트폴리오':>12}{'벤치마크':>12}")
    for k in ("누적수익률", "연환산수익률", "연변동성", "최대낙폭", "승률", "샤프"):
        pv, bv = p.get(k), b.get(k)
        unit = "" if k == "샤프" else "%"
        pv = f"{pv}{unit}" if pv is not None else "-"
        bv = f"{bv}{unit}" if bv is not None else "-"
        print(f"  {k:<14}{pv:>12}{bv:>12}")
    print(f"\n  초과수익률: {s['초과수익률']}%p")
    if s.get("통계"):
        print("\n  통계 (t = IR x √기간):")
        for k, v in s["통계"].items():
            print(f"   · {k}: {v}")
    if len(res.get("yearly", [])):
        print("\n  연도별 (%):")
        print("   " + res["yearly"].to_string(index=False).replace("\n", "\n   "))
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
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    reb = sys.argv[3] if len(sys.argv) > 3 else ""
    cost = float(sys.argv[4]) if len(sys.argv) > 4 else -1.0
    cap = float(sys.argv[5]) if len(sys.argv) > 5 else -1.0
    turn = float(sys.argv[6]) if len(sys.argv) > 6 else -1.0
    tr = int(sys.argv[7]) if len(sys.argv) > 7 else 1
    tma = int(sys.argv[8]) if len(sys.argv) > 8 else 0
    roff = float(sys.argv[9]) if len(sys.argv) > 9 else 0.0
    wgt = sys.argv[10] if len(sys.argv) > 10 else ""
    mw = float(sys.argv[11]) if len(sys.argv) > 11 else -1.0
    bnd = float(sys.argv[12]) if len(sys.argv) > 12 else -1.0
    buf = float(sys.argv[13]) if len(sys.argv) > 13 else -1.0
    sn = sys.argv[14] if len(sys.argv) > 14 else ""
    lv = int(sys.argv[15]) if len(sys.argv) > 15 else -1
    ts = int(sys.argv[16]) if len(sys.argv) > 16 else -1
    sw = float(sys.argv[17]) if len(sys.argv) > 17 else -1.0
    qw = sys.argv[18] if len(sys.argv) > 18 else ""
    spl = sys.argv[19] if len(sys.argv) > 19 else ""
    sys.exit(main(market, top_n, reb, cost, cap, turn, tr, tma, roff,
                  wgt, mw, bnd, buf, sn, lv, ts, sw, qw, spl))
