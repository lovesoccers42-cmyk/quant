# -*- coding: utf-8 -*-
"""미국 주가 수집 (yfinance).

노트북은 종목을 하나씩 받았지만 6,000종목이면 몇 시간짜리라
yfinance의 묶음 다운로드로 바꿨습니다. 한 번에 100종목씩 받습니다.
"""
import logging
from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta

import config
import store

log = logging.getLogger("quant_agent.global_price")

COLUMNS = ["Date", "High", "Low", "Open", "Close", "Volume", "Symbol"]


def _session():
    """curl_cffi로 브라우저를 흉내 냅니다. Yahoo의 데이터센터 IP 차단 완화용."""
    try:
        from curl_cffi import requests as cffi
        return cffi.Session(impersonate="chrome")
    except Exception:
        return None


def download(symbols: list[str], fr: str, to: str, **extra) -> pd.DataFrame:
    """yfinance 묶음 다운로드. actions=True를 주면 배당·분할 컬럼도 옵니다."""
    import yfinance as yf

    kw = dict(start=fr, end=to, progress=False, group_by="ticker",
              auto_adjust=False, threads=True)
    kw.update(extra)

    sess = _session()
    if sess is not None:
        try:
            return yf.download(symbols, session=sess, **kw)
        except TypeError:
            # yfinance 버전에 따라 session 인자를 받지 않습니다
            pass
        except Exception as e:
            log.debug("세션 다운로드 실패, 기본 경로로 재시도: %s", e)
    return yf.download(symbols, **kw)


def _to_long(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """yfinance 결과를 (Date, OHLCV, Symbol) 행 단위로 폅니다."""
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)

    if isinstance(df.columns, pd.MultiIndex):
        try:
            long = df.stack(level=0, future_stack=True)
        except TypeError:                      # pandas < 2.1
            long = df.stack(level=0)
        long.index = long.index.set_names(["Date", "Symbol"])
        long = long.reset_index()
    else:
        long = df.reset_index()
        long["Symbol"] = symbols[0]
        if "Date" not in long.columns and "index" in long.columns:
            long = long.rename(columns={"index": "Date"})

    for col in COLUMNS:
        if col not in long.columns:
            long[col] = pd.NA

    long = long[COLUMNS].copy()
    long["Date"] = pd.to_datetime(long["Date"], errors="coerce", utc=True).dt.tz_localize(None)
    for col in ("High", "Low", "Open", "Close", "Volume"):
        long[col] = pd.to_numeric(long[col], errors="coerce")

    return long.dropna(subset=["Date", "Close"])


def collect(days: int | None = None, years: int | None = None) -> dict:
    if days:
        fr = (date.today() + relativedelta(days=-days)).strftime("%Y-%m-%d")
    elif years:
        fr = (date.today() + relativedelta(years=-years)).strftime("%Y-%m-%d")
    else:
        raise ValueError("days 또는 years를 지정하세요.")
    to = date.today().strftime("%Y-%m-%d")

    symbols = store.us_symbols()
    if not symbols:
        raise RuntimeError("global_ticker가 비어 있습니다. 종목 수집을 먼저 실행하세요.")

    chunk_size = config.YF_CHUNK
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]

    errors, total_rows, got_symbols = [], 0, set()
    buffer = []

    for n, chunk in enumerate(chunks, 1):
        try:
            raw = download(chunk, fr, to)
            long = _to_long(raw, chunk)
        except Exception as e:
            log.warning("묶음 %d/%d 실패: %s", n, len(chunks), e)
            errors.extend(chunk)
            continue

        if long.empty:
            errors.extend(chunk)
        else:
            got = set(long["Symbol"].unique())
            got_symbols |= got
            errors.extend([s for s in chunk if s not in got])
            buffer.append(long)

        if len(buffer) >= 5:
            total_rows += store.upsert("global_price", pd.concat(buffer, ignore_index=True))
            buffer = []

        log.info("미국 주가 %d/%d 묶음 (수집 %d종목, 실패 %d)",
                 n, len(chunks), len(got_symbols), len(errors))
        import time
        time.sleep(config.YF_SLEEP)

    if buffer:
        total_rows += store.upsert("global_price", pd.concat(buffer, ignore_index=True))

    kept = store.prune_price("global_price")

    return {"tickers": len(symbols), "collected": len(got_symbols),
            "rows": total_rows, "stored_rows": kept, "errors": errors,
            "error_rate": round(len(errors) / max(len(symbols), 1), 4)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = collect(years=config.MONTHLY_PRICE_YEARS)
    print({k: v for k, v in r.items() if k != "errors"})
