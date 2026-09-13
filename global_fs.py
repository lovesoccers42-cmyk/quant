# -*- coding: utf-8 -*-
"""미국 재무제표 수집.

1순위 yfinance, 2순위 yahooquery.

노트북은 yahooquery만 썼는데, GitHub Actions에서 돌려보니 전 종목이 실패했습니다
(Yahoo가 fundamentals 계열 엔드포인트에 crumb/cookie 인증을 요구하는데
yahooquery가 이를 따라가지 못하는 것으로 보입니다). 같은 실행에서 yfinance의
차트 다운로드는 정상이었으므로, 인증을 처리하는 yfinance를 먼저 쓰고
yahooquery는 대비책으로 남겨 뒀습니다.

계정명은 yahooquery와 같은 표기를 씁니다(pretty=False).
"""
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import config
import store

log = logging.getLogger("quant_agent.global_fs")

COLUMNS = ["Symbol", "date", "account", "value", "freq"]
DROP_COLS = {"periodType", "currencyCode"}

NEEDED = ["NetIncome", "TotalRevenue", "GrossProfit",
          "StockholdersEquity", "TotalAssets",
          "CashFlowFromContinuingOperatingActivities"]

# 계정명이 판마다 조금씩 달라 대체 이름을 허용합니다
ALIASES = {
    "CashFlowFromContinuingOperatingActivities": ["OperatingCashFlow"],
    "NetIncome": ["NetIncomeCommonStockholders", "NetIncomeContinuousOperations"],
    "StockholdersEquity": ["TotalEquityGrossMinorityInterest", "CommonStockEquity"],
}


def _apply_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """필요한 계정이 없고 대체 이름이 있으면 그 이름으로 채웁니다."""
    if df.empty:
        return df
    have = set(df["account"].unique())
    adds = []
    for want, alts in ALIASES.items():
        if want in have:
            continue
        for alt in alts:
            if alt in have:
                sub = df[df["account"] == alt].copy()
                sub["account"] = want
                adds.append(sub)
                break
    return pd.concat([df] + adds, ignore_index=True) if adds else df


# ── 1순위: yfinance ──────────────────────────────────────────
def _melt_yf(raw, symbol: str, freq: str) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame(columns=COLUMNS)
    out = raw.copy()
    out.index = out.index.astype(str)
    out.index.name = "account"
    out = out.reset_index().melt(id_vars="account", var_name="date", value_name="value")
    out["Symbol"] = symbol
    out["freq"] = freq
    return out[COLUMNS]


def _fetch_yf(symbol: str) -> pd.DataFrame:
    import yfinance as yf

    t = yf.Ticker(symbol)
    frames = []
    for yf_freq, code in (("yearly", "y"), ("quarterly", "q")):
        for getter in ("get_income_stmt", "get_balance_sheet", "get_cashflow"):
            try:
                raw = getattr(t, getter)(freq=yf_freq, pretty=False)
            except Exception as e:
                log.debug("%s %s/%s 실패: %s", symbol, getter, yf_freq, e)
                continue
            frames.append(_melt_yf(raw, symbol, code))

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)


# ── 2순위: yahooquery ────────────────────────────────────────
def _melt_yq(raw, freq: str) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = raw.reset_index()
    if "symbol" not in df.columns or "asOfDate" not in df.columns:
        return pd.DataFrame(columns=COLUMNS)
    df = df.loc[:, [c for c in df.columns if c not in DROP_COLS]]
    df = df.melt(id_vars=["symbol", "asOfDate"], var_name="account", value_name="value")
    df = df.rename(columns={"symbol": "Symbol", "asOfDate": "date"})
    df["freq"] = freq
    return df[COLUMNS]


def _fetch_yq(symbols: list[str]) -> pd.DataFrame:
    from yahooquery import Ticker

    t = Ticker(symbols, asynchronous=True, progress=False,
               max_workers=config.US_FS_WORKERS, validate=False)
    frames = []
    for yq_freq, code in (("a", "y"), ("q", "q")):
        try:
            frames.append(_melt_yq(t.all_financial_data(frequency=yq_freq), code))
        except Exception as e:
            log.warning("yahooquery(%s) 실패: %s", yq_freq, e)
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)


# ── 공통 정리 ────────────────────────────────────────────────
def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = _apply_aliases(df)
    df = df[df["account"].isin(NEEDED)]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_localize(None)
    df = df.dropna(subset=["value", "date"])
    return df.drop_duplicates(["Symbol", "date", "account", "freq"])[COLUMNS]


def collect() -> dict:
    symbols = store.us_symbols()
    if not symbols:
        raise RuntimeError("global_ticker가 비어 있습니다.")

    buffer, total_rows, got = [], 0, set()
    reasons = Counter()

    # 1순위 — yfinance (종목별 조회, 스레드 병렬)
    with ThreadPoolExecutor(max_workers=config.US_FS_WORKERS) as pool:
        futures = {pool.submit(_fetch_yf, s): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                df = _tidy(fut.result())
            except Exception as e:
                reasons[type(e).__name__] += 1
                df = pd.DataFrame(columns=COLUMNS)

            if len(df):
                got.add(sym)
                buffer.append(df)
            else:
                reasons["빈응답"] += 1

            done += 1
            if len(buffer) >= 300:
                total_rows += store.upsert("global_fs", pd.concat(buffer, ignore_index=True))
                buffer = []
            if done % 500 == 0:
                log.info("미국 재무제표(yfinance) %d/%d (수집 %d종목)",
                         done, len(symbols), len(got))

    if buffer:
        total_rows += store.upsert("global_fs", pd.concat(buffer, ignore_index=True))
        buffer = []

    source = "yfinance"
    log.info("yfinance 결과: %d/%d 종목 (실패 사유 %s)",
             len(got), len(symbols), dict(reasons.most_common(5)))

    # 2순위 — 절반도 못 받았으면 yahooquery로 나머지를 시도
    missing = [s for s in symbols if s not in got]
    if len(got) < len(symbols) * 0.5 and missing:
        log.warning("yfinance 수집률이 낮아 yahooquery로 %d종목 재시도합니다.", len(missing))
        size = config.FS_CHUNK
        chunks = [missing[i:i + size] for i in range(0, len(missing), size)]
        for n, chunk in enumerate(chunks, 1):
            try:
                df = _tidy(_fetch_yq(chunk))
            except Exception as e:
                log.warning("yahooquery 묶음 %d 실패: %s", n, e)
                continue
            if len(df):
                got |= set(df["Symbol"].unique())
                buffer.append(df)
            if len(buffer) >= 6:
                total_rows += store.upsert("global_fs", pd.concat(buffer, ignore_index=True))
                buffer = []
        if buffer:
            total_rows += store.upsert("global_fs", pd.concat(buffer, ignore_index=True))
        if len(got) > 0:
            source = "yfinance+yahooquery"

    errors = [s for s in symbols if s not in got]
    return {"source": source, "tickers": len(symbols), "collected": len(got),
            "rows": total_rows, "fail_reasons": dict(reasons.most_common(5)),
            "errors": errors,
            "error_rate": round(len(errors) / max(len(symbols), 1), 4)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = collect()
    print({k: v for k, v in r.items() if k != "errors"})
