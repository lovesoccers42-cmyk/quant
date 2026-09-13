# -*- coding: utf-8 -*-
"""미국 밸류 지표 (PER/PBR/PCR/PSR — TTM 기반, DY).

배당수익률은 노트북에서 종목당 yfinance를 한 번씩 불러 몇 시간이 걸렸습니다.
yahooquery의 summary_detail은 심볼을 묶어 받을 수 있어 그 방식으로 바꿨습니다.
"""
import logging
import time
from datetime import date

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

import config
import factor_core as fc
import store

log = logging.getLogger("quant_agent.global_value")

ACCOUNT_TO_METRIC = {
    "TotalRevenue": "PSR",
    "CashFlowFromContinuingOperatingActivities": "PCR",
    "StockholdersEquity": "PBR",
    "NetIncome": "PER",
}
MEAN_ACCOUNTS = ("StockholdersEquity",)


def _dy_from_dividends(symbols: list[str]) -> pd.Series:
    """최근 12개월 배당 합계 ÷ 종가 × 100.

    노트북의 방식 ①과 같습니다. yahooquery의 summary_detail은 GitHub Actions에서
    전 종목 실패했지만, 배당은 주가와 같은 차트 엔드포인트로 받을 수 있어
    (actions=True) 그쪽이 훨씬 안정적입니다.
    """
    import global_price

    fr = (date.today() + relativedelta(years=-1)).strftime("%Y-%m-%d")
    to = date.today().strftime("%Y-%m-%d")

    out = {}
    size = config.YF_CHUNK
    chunks = [symbols[i:i + size] for i in range(0, len(symbols), size)]

    for n, chunk in enumerate(chunks, 1):
        try:
            raw = global_price.download(chunk, fr, to, actions=True)
        except Exception as e:
            log.warning("배당 묶음 %d/%d 실패: %s", n, len(chunks), e)
            continue

        if raw is None or raw.empty:
            continue

        multi = isinstance(raw.columns, pd.MultiIndex)
        for sym in chunk:
            try:
                if multi:
                    if sym not in raw.columns.get_level_values(0):
                        continue
                    sub = raw[sym]
                else:
                    sub = raw
                if "Dividends" not in sub.columns or "Close" not in sub.columns:
                    continue
                div = pd.to_numeric(sub["Dividends"], errors="coerce").fillna(0).sum()
                close = pd.to_numeric(sub["Close"], errors="coerce").dropna()
                if div > 0 and len(close) and close.iloc[-1] > 0:
                    out[sym] = round(float(div) / float(close.iloc[-1]) * 100, 4)
            except Exception:
                continue

        log.info("배당 수집 %d/%d 묶음 (누적 %d종목)", n, len(chunks), len(out))
        time.sleep(config.YF_SLEEP)

    return pd.Series(out, dtype="float64")


def _dy_from_yahooquery(symbols: list[str]) -> pd.Series:
    """대비책 — yahooquery summary_detail 묶음 조회."""
    from yahooquery import Ticker

    out = {}
    size = config.FS_CHUNK
    chunks = [symbols[i:i + size] for i in range(0, len(symbols), size)]

    for n, chunk in enumerate(chunks, 1):
        try:
            detail = Ticker(chunk, asynchronous=True, progress=False,
                            max_workers=config.US_FS_WORKERS,
                            validate=False).summary_detail
        except Exception as e:
            log.debug("배당(yahooquery) 묶음 %d 실패: %s", n, e)
            continue
        if not isinstance(detail, dict):
            continue
        for sym, info in detail.items():
            if not isinstance(info, dict):
                continue
            dy = info.get("dividendYield")
            try:
                dy = float(dy)
            except (TypeError, ValueError):
                continue
            if dy > 0:
                # 버전에 따라 비율(0.0044)과 %(0.44)가 섞여 옵니다
                out[sym] = round(dy if dy > 1 else dy * 100, 4)
    return pd.Series(out, dtype="float64")


def _dividend_yield(symbols: list[str]) -> pd.Series:
    dy = _dy_from_dividends(symbols)
    if len(dy) < max(len(symbols) * 0.05, 20):
        log.warning("배당 수집이 %d종목뿐 — yahooquery로 보완합니다.", len(dy))
        alt = _dy_from_yahooquery(symbols)
        if len(alt):
            dy = alt.combine_first(dy) if len(dy) else alt
    return dy


def build() -> dict:
    fs = store.read_sql("""
        select * from global_fs
        where freq = 'q'
          and account in ('NetIncome','StockholdersEquity',
                          'CashFlowFromContinuingOperatingActivities','TotalRevenue');
    """)
    ticker_list = store.read_sql("""
        select * from global_ticker
        where date = (select max(date) from global_ticker);
    """)

    if fs.empty:
        raise RuntimeError("global_fs가 비어 있습니다. 미국 월간 수집을 먼저 실행하세요.")

    # TTM (자본은 평균, 분기 연속성 검사 포함)
    fs = fc.latest_ttm(fs, symbol="Symbol", account="account",
                       date="date", value="value", mean_accounts=MEAN_ACCOUNTS)

    merged = fs[["account", "Symbol", "ttm"]].merge(
        ticker_list[["Symbol", "Market Cap", "date"]], on="Symbol")
    merged["Market Cap"] = pd.to_numeric(merged["Market Cap"], errors="coerce")

    merged["값"] = (merged["Market Cap"] / merged["ttm"]).round(4)
    merged["지표"] = merged["account"].map(ACCOUNT_TO_METRIC)
    merged = merged[["Symbol", "date", "지표", "값"]]
    merged = merged.replace([np.inf, -np.inf], np.nan).dropna(subset=["값", "지표"])
    n_value = store.upsert("global_value", merged)

    # 배당수익률
    symbols = store.us_symbols()
    dy = _dividend_yield(symbols)
    n_dy = 0
    if len(dy):
        as_of = ticker_list["date"].max()
        dy_df = pd.DataFrame({"Symbol": dy.index, "date": as_of,
                              "지표": "DY", "값": dy.values})
        n_dy = store.upsert("global_value", dy_df)

    return {"value_rows": n_value, "dy_rows": n_dy, "dy_symbols": int(len(dy))}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build())
