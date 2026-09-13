# -*- coding: utf-8 -*-
"""미국 밸류 지표 (PER/PBR/PCR/PSR — TTM 기반, DY).

배당수익률은 노트북에서 종목당 yfinance를 한 번씩 불러 몇 시간이 걸렸습니다.
yahooquery의 summary_detail은 심볼을 묶어 받을 수 있어 그 방식으로 바꿨습니다.
"""
import logging
import time

import numpy as np
import pandas as pd

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


def _dividend_yield(symbols: list[str]) -> pd.Series:
    """심볼별 배당수익률(%) — yahooquery summary_detail 묶음 조회."""
    from yahooquery import Ticker

    out = {}
    size = config.FS_CHUNK
    chunks = [symbols[i:i + size] for i in range(0, len(symbols), size)]

    for n, chunk in enumerate(chunks, 1):
        try:
            t = Ticker(chunk, asynchronous=True, progress=False,
                       max_workers=config.FS_WORKERS, validate=False,
                       retry=2, timeout=60)
            detail = t.summary_detail
        except Exception as e:
            log.debug("배당 묶음 %d 실패: %s", n, e)
            continue

        if not isinstance(detail, dict):
            continue

        for sym, info in detail.items():
            if not isinstance(info, dict):
                continue
            dy = info.get("dividendYield")
            if dy in (None, "", 0):
                continue
            try:
                dy = float(dy)
            except (TypeError, ValueError):
                continue
            if dy <= 0:
                continue
            # yfinance/yahooquery 버전에 따라 비율(0.0044)과 %(0.44)가 섞여 옵니다
            out[sym] = round(dy if dy > 1 else dy * 100, 4)

        log.info("배당 수집 %d/%d 묶음 (누적 %d종목)", n, len(chunks), len(out))
        time.sleep(config.FS_SLEEP)

    return pd.Series(out, dtype="float64")


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
