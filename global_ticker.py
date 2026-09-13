# -*- coding: utf-8 -*-
"""미국 상장 종목 수집 (NASDAQ / NYSE / AMEX).

노트북에서는 nasdaq.com에서 스크리너 CSV 3개를 손으로 받아 읽었습니다.
클라우드에서는 사람이 없으니 같은 화면이 쓰는 공개 API를 직접 호출합니다.
API가 막히면 data/ 폴더의 nasdaq_screener*.csv 로 대체합니다.
"""
import logging
from datetime import date

import numpy as np
import pandas as pd

import config
import http_util
import store

log = logging.getLogger("quant_agent.global_ticker")

EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
API = "https://api.nasdaq.com/api/screener/stocks"
REFERER = "https://www.nasdaq.com/market-activity/stocks/screener"

COLUMNS = ["Name", "Symbol", "Exchange", "Sector",
           "Market Cap", "Dividend", "country", "date"]


def _fetch_exchange(exchange: str) -> pd.DataFrame:
    """한 거래소의 전 종목. download=true면 페이지 없이 통째로 옵니다."""
    resp = http_util.get(
        API, referer=REFERER,
        params={"tableonly": "false", "download": "true",
                "exchange": exchange, "limit": "25000"},
        headers={"Accept": "application/json, text/plain, */*"},
    )
    rows = (resp.json().get("data") or {}).get("rows") or []
    if not rows:
        raise RuntimeError(f"{exchange}: 빈 응답")

    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "Name": df.get("name"),
        "Symbol": df.get("symbol"),
        "Sector": df.get("sector"),
        "Market Cap": df.get("marketCap"),
        "country": df.get("country"),
    })
    out["Exchange"] = exchange
    return out


def _from_csv() -> pd.DataFrame:
    """대체 경로 — data/ 에 넣어둔 nasdaq 스크리너 CSV를 읽습니다."""
    files = sorted(config.DATA_DIR.glob("nasdaq_screener*.csv"))
    if not files:
        return pd.DataFrame()

    frames = []
    for f in files:
        raw = pd.read_csv(f)
        cols = {c.lower().strip(): c for c in raw.columns}

        def pick(*names):
            for n in names:
                if n in cols:
                    return raw[cols[n]]
            return pd.Series([None] * len(raw))

        # 파일명에 거래소가 들어 있으면 쓰고, 없으면 Unknown
        name_up = f.name.upper()
        exch = next((e for e in EXCHANGES if e in name_up), "Unknown")

        frames.append(pd.DataFrame({
            "Name": pick("name"),
            "Symbol": pick("symbol"),
            "Sector": pick("sector"),
            "Market Cap": pick("market cap", "marketcap"),
            "country": pick("country"),
            "Exchange": exch,
        }))
        log.info("CSV 대체 경로: %s (%d행, 거래소 %s)", f.name, len(raw), exch)

    return pd.concat(frames, ignore_index=True)


def _clean(df: pd.DataFrame, biz_day: str | None = None) -> pd.DataFrame:
    df = df.copy()

    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    df = df[df["Symbol"].notna() & (df["Symbol"] != "") & (df["Symbol"] != "nan")]

    # "$3,050,000,000" 같은 표기도 있어 문자 제거 후 숫자화
    mc = df["Market Cap"].astype(str).str.replace(r"[^0-9.\-]", "", regex=True)
    df["Market Cap"] = pd.to_numeric(mc, errors="coerce")
    df = df[df["Market Cap"].notna() & (df["Market Cap"] != 0)]

    df["Name"] = df["Name"].astype(str).str.slice(0, 50)
    df["Sector"] = df["Sector"].replace("", np.nan).fillna("Unknown")
    df["country"] = df["country"].astype(str).str.slice(0, 20).replace(
        {"": "United States", "nan": "United States", "None": "United States"})
    df["Dividend"] = None
    df["date"] = pd.to_datetime(biz_day) if biz_day else pd.to_datetime(date.today())

    # 워런트/유닛/권리 등 비정상 종목 제외 (노트북의 배당 수집 필터와 동일 기준)
    bad_suffix = (".W", ".WS", ".WT", ".U", ".R", "-WS", "-WT", "-U", "-R")
    df = df[~df["Symbol"].str.startswith("$")]
    df = df[~df["Symbol"].str.upper().str.endswith(bad_suffix)]

    df = df.drop_duplicates(["Symbol"]).reset_index(drop=True)
    return df[COLUMNS]


def collect(biz_day: str | None = None) -> dict:
    frames, failed = [], []

    for exchange in EXCHANGES:
        try:
            frames.append(_fetch_exchange(exchange))
            log.info("%s 수집 완료", exchange)
        except Exception as e:
            log.warning("%s 수집 실패: %s", exchange, e)
            failed.append(exchange)
        http_util.polite_sleep(1.0)

    source = "nasdaq-api"
    if not frames:
        df = _from_csv()
        source = "csv"
        if df.empty:
            raise RuntimeError(
                "미국 종목 목록을 받지 못했습니다. nasdaq.com API가 막혔을 수 있습니다. "
                "nasdaq.com 스크리너에서 CSV를 받아 data/ 폴더에 "
                "nasdaq_screener_NASDAQ.csv 처럼 넣고 다시 실행하세요."
            )
    else:
        df = pd.concat(frames, ignore_index=True)

    df = _clean(df, biz_day)
    if len(df) < 500:
        raise RuntimeError(f"수집된 미국 종목이 {len(df)}개뿐입니다 — 응답이 잘린 것 같습니다.")

    n = store.upsert("global_ticker", df)
    us = int((df["country"] == "United States").sum())

    return {"source": source, "rows": n, "us_stocks": us,
            "exchanges_failed": failed,
            "by_exchange": df["Exchange"].value_counts().to_dict()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(collect())
