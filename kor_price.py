# -*- coding: utf-8 -*-
"""네이버 금융 일별 주가 수집 (병렬 + 일괄 저장)."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from io import StringIO

import pandas as pd
from dateutil.relativedelta import relativedelta

import config
import http_util
import store

log = logging.getLogger("quant_agent.price")

REFERER = "https://finance.naver.com/"
CHUNK = 400          # 이 종목 수마다 중간 저장 (중단돼도 진행분 보존)


def _fetch_one(ticker: str, fr: str, to: str) -> pd.DataFrame:
    url = (f"https://fchart.stock.naver.com/siseJson.nhn?symbol={ticker}"
           f"&requestType=1&startTime={fr}&endTime={to}&timeframe=day")
    text = http_util.get(url, referer=REFERER).text
    cleaned = text.replace("[", "").replace("]", "").replace('"', "")

    raw = pd.read_csv(StringIO(cleaned))
    price = raw.iloc[:, 0:6].copy()
    price.columns = ["날짜", "시가", "고가", "저가", "종가", "거래량"]
    price = price.dropna()
    price["날짜"] = price["날짜"].astype(str).str.extract(r"(\d+)")
    price["날짜"] = pd.to_datetime(price["날짜"], format="%Y%m%d")
    price["종목코드"] = ticker
    for col in ("시가", "고가", "저가", "종가", "거래량"):
        price[col] = pd.to_numeric(price[col], errors="coerce")
    return price.dropna(subset=["날짜", "종가"])


def _fetch_with_retry(ticker: str, fr: str, to: str):
    try:
        http_util.polite_sleep(config.PRICE_SLEEP)
        df = _fetch_one(ticker, fr, to)
        if df.empty:
            return ticker, None
        return ticker, df
    except Exception as e:
        log.debug("주가 수집 실패 %s: %s", ticker, e)
        return ticker, None


def collect(days: int | None = None, years: int | None = None) -> dict:
    """days 또는 years 중 하나 지정."""
    if days:
        fr = (date.today() + relativedelta(days=-days)).strftime("%Y%m%d")
    elif years:
        fr = (date.today() + relativedelta(years=-years)).strftime("%Y%m%d")
    else:
        raise ValueError("days 또는 years를 지정하세요.")
    to = date.today().strftime("%Y%m%d")

    tickers = store.common_tickers()
    if not tickers:
        raise RuntimeError("kor_ticker가 비어 있습니다. 티커 수집을 먼저 실행하세요.")

    errors, buffer, total_rows = [], [], 0

    with ThreadPoolExecutor(max_workers=config.PRICE_WORKERS) as pool:
        futures = {pool.submit(_fetch_with_retry, t, fr, to): t for t in tickers}
        done = 0
        for fut in as_completed(futures):
            ticker, df = fut.result()
            if df is None:
                errors.append(ticker)
            else:
                buffer.append(df)
            done += 1

            if len(buffer) >= CHUNK:
                total_rows += store.upsert("kor_price", pd.concat(buffer, ignore_index=True))
                buffer = []
                log.info("주가 수집 %d/%d (실패 %d)", done, len(tickers), len(errors))

    if buffer:
        total_rows += store.upsert("kor_price", pd.concat(buffer, ignore_index=True))

    kept = store.prune_price("kor_price")

    return {"tickers": len(tickers), "rows": total_rows, "stored_rows": kept,
            "errors": errors,
            "error_rate": round(len(errors) / max(len(tickers), 1), 4)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print({k: v for k, v in collect(days=config.DAILY_PRICE_DAYS).items() if k != "errors"})
