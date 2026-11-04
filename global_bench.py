# -*- coding: utf-8 -*-
"""미국 벤치마크(S&P 500 = SPY) 수집.

왜 종목 유니버스와 **따로** 두는가
  global_ticker / global_price는 QVM 모델이 점수를 매기는 종목 명단입니다.
  SPY를 거기에 넣으면 모델이 ETF를 종목으로 보고 순위에 올리고, 어느 달엔
  주문서에 "SPY 매수"가 섞여 나옵니다. 지수는 비교 대상이고 매수 후보가
  아니므로 테이블을 갈라 둡니다.

왜 수정종가인가
  실제 계좌 수익률에는 받은 배당이 들어 있습니다(예수금으로 들어와
  총자본에 잡힙니다). 그래서 비교 대상도 배당을 재투자한 총수익이어야
  공정합니다. SPY 배당은 연 1.2~1.5% 수준이라, 단순 종가로 비교하면
  매년 그만큼 지수를 쉽게 이기는 것처럼 보입니다.

이름이 global_* 인 이유: 공개 릴리스 업로드가 `data/global_*.parquet`
글롭으로 묶여 있어 자동으로 따라 올라갑니다.
"""
import logging
from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta

import global_price
import store

log = logging.getLogger("quant_agent.global_bench")

# 지금은 SPY 하나입니다. 테이블 키가 (Date, Symbol)이라 나중에 QQQ 같은 걸
# 더해도 스키마를 바꿀 필요가 없습니다.
SYMBOLS = ["SPY"]
COLUMNS = ["Date", "Symbol", "Close", "AdjClose"]


def _to_long(raw: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """yfinance 결과에서 종가와 수정종가만 남깁니다."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLUMNS)

    if isinstance(raw.columns, pd.MultiIndex):
        try:
            long = raw.stack(level=0, future_stack=True)
        except TypeError:                      # pandas < 2.1
            long = raw.stack(level=0)
        long.index = long.index.set_names(["Date", "Symbol"])
        long = long.reset_index()
    else:
        long = raw.reset_index()
        long["Symbol"] = symbols[0]
        if "Date" not in long.columns and "index" in long.columns:
            long = long.rename(columns={"index": "Date"})

    # yfinance 버전에 따라 'Adj Close' / 'Adj_Close' 로 옵니다. 없으면
    # 종가로 대체하되 **조용히 넘어가지 않고** 로그를 남깁니다 — 배당이
    # 빠진 지수와 비교하면 성과가 과대평가됩니다.
    adj = next((c for c in long.columns
                if str(c).replace("_", " ").strip().lower() == "adj close"),
               None)
    if adj is None:
        log.warning("수정종가 칸이 없습니다 — 종가로 대체합니다 "
                    "(배당 제외, 지수가 실제보다 낮게 보입니다)")
        long["AdjClose"] = long.get("Close")
    else:
        long["AdjClose"] = long[adj]

    for col in COLUMNS:
        if col not in long.columns:
            long[col] = pd.NA

    out = long[COLUMNS].copy()
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce",
                                 utc=True).dt.tz_localize(None)
    out["Symbol"] = out["Symbol"].astype(str).str.strip().str.upper()
    for col in ("Close", "AdjClose"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["Date", "AdjClose"])


def collect(days: int | None = None, years: int | None = None) -> dict:
    if days:
        fr = (date.today() + relativedelta(days=-days)).strftime("%Y-%m-%d")
    elif years:
        fr = (date.today() + relativedelta(years=-years)).strftime("%Y-%m-%d")
    else:
        raise ValueError("days 또는 years를 지정하세요.")
    to = date.today().strftime("%Y-%m-%d")

    raw = global_price.download(SYMBOLS, fr, to)
    long = _to_long(raw, SYMBOLS)
    if long.empty:
        # 지수가 없으면 대시보드가 '비교 없음'으로 떨어집니다. 파이프라인을
        # 세울 일은 아니라서 critical로 쓰지 않습니다.
        log.warning("벤치마크를 받지 못했습니다 (%s~%s)", fr, to)
        return {"rows": 0, "symbols": 0, "errors": list(SYMBOLS)}

    rows = store.upsert("global_bench", long)
    got = sorted(set(long["Symbol"]))
    log.info("벤치마크 %d행 (%s · %s~%s)", rows, ", ".join(got),
             f"{long['Date'].min():%Y-%m-%d}", f"{long['Date'].max():%Y-%m-%d}")
    return {"rows": rows, "symbols": len(got),
            "errors": [s for s in SYMBOLS if s not in set(got)],
            "first": f"{long['Date'].min():%Y-%m-%d}",
            "last": f"{long['Date'].max():%Y-%m-%d}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import config
    print(collect(years=config.US_MONTHLY_PRICE_YEARS))
