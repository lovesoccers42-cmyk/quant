# -*- coding: utf-8 -*-
"""미국 재무제표 수집 (yahooquery).

노트북은 Ticker(하나)를 6,000번 호출했지만, yahooquery는 심볼 여러 개를
한 번에 받습니다. 50개씩 묶어 호출 수를 100분의 1로 줄였습니다.
"""
import logging
import time

import pandas as pd

import config
import store

log = logging.getLogger("quant_agent.global_fs")

DROP_COLS = {"periodType", "currencyCode"}
COLUMNS = ["Symbol", "date", "account", "value", "freq"]


def _tidy(raw, freq: str) -> pd.DataFrame:
    """yahooquery 결과를 (Symbol, date, account, value, freq) 행으로 폅니다."""
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame(columns=COLUMNS)

    df = raw.reset_index()
    if "symbol" not in df.columns:
        return pd.DataFrame(columns=COLUMNS)
    if "asOfDate" not in df.columns:
        return pd.DataFrame(columns=COLUMNS)

    df = df.loc[:, [c for c in df.columns if c not in DROP_COLS]]
    df = df.melt(id_vars=["symbol", "asOfDate"],
                 var_name="account", value_name="value")

    df = df.rename(columns={"symbol": "Symbol", "asOfDate": "date"})
    df["freq"] = freq
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    return df.dropna(subset=["value", "date"])[COLUMNS]


def _fetch_chunk(symbols: list[str]) -> pd.DataFrame:
    from yahooquery import Ticker

    t = Ticker(symbols, asynchronous=True, progress=False,
               max_workers=config.FS_WORKERS, validate=False,
               retry=2, timeout=60)

    frames = []
    for freq, code in (("a", "y"), ("q", "q")):
        try:
            frames.append(_tidy(t.all_financial_data(frequency=freq), code))
        except Exception as e:
            log.debug("재무제표(%s) 실패: %s", freq, e)

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)


def collect() -> dict:
    symbols = store.us_symbols()
    if not symbols:
        raise RuntimeError("global_ticker가 비어 있습니다.")

    size = config.FS_CHUNK
    chunks = [symbols[i:i + size] for i in range(0, len(symbols), size)]

    buffer, total_rows, got = [], 0, set()

    for n, chunk in enumerate(chunks, 1):
        try:
            df = _fetch_chunk(chunk)
        except Exception as e:
            log.warning("재무제표 묶음 %d/%d 실패: %s", n, len(chunks), e)
            df = pd.DataFrame(columns=COLUMNS)

        if len(df):
            got |= set(df["Symbol"].unique())
            buffer.append(df)

        if len(buffer) >= 6:
            total_rows += store.upsert("global_fs", pd.concat(buffer, ignore_index=True))
            buffer = []

        log.info("미국 재무제표 %d/%d 묶음 (수집 %d종목)", n, len(chunks), len(got))
        time.sleep(config.FS_SLEEP)

    if buffer:
        total_rows += store.upsert("global_fs", pd.concat(buffer, ignore_index=True))

    errors = [s for s in symbols if s not in got]
    return {"tickers": len(symbols), "collected": len(got), "rows": total_rows,
            "errors": errors,
            "error_rate": round(len(errors) / max(len(symbols), 1), 4)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = collect()
    print({k: v for k, v in r.items() if k != "errors"})
