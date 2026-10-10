# -*- coding: utf-8 -*-
"""데이터 저장소 — MySQL 대체.

각 테이블은 data/<table>.parquet 파일 하나로 저장되고,
조회는 DuckDB가 그 parquet들을 뷰로 올려서 처리합니다.
기존 MySQL SQL(한글 컬럼명, interval 문법)이 거의 그대로 동작합니다.

사용:
    store.upsert("kor_ticker", df)          # 기본키 기준 덮어쓰기
    store.read_sql("select * from kor_ticker where ...")
"""
from __future__ import annotations

import threading

import duckdb
import pandas as pd

import config

# 테이블별 기본키 (MySQL primary key와 동일)
PRIMARY_KEYS: dict[str, list[str]] = {
    # 한국장
    "kor_ticker": ["종목코드", "기준일"],
    "kor_sector": ["CMP_CD", "기준일"],
    "kor_price": ["날짜", "종목코드"],
    "kor_fs": ["계정", "기준일", "종목코드", "공시구분"],
    "kor_value": ["종목코드", "기준일", "지표"],
    "kor_portfolio": ["종목코드"],        # 실전 보유 상태 (분할 리밸런싱)
    "kor_capital": ["기준일"],           # 운용자금 (월 1회 재동기화)
    "kor_portfolio_alt": ["종목코드"],
    "kor_capital_alt": ["기준일"],
    # 거래·보유 기록 (덮어쓰지 않고 쌓습니다 — 성과 계산의 원장)
    "kor_trades": ["기준일", "계좌", "종목코드", "구분"],
    "kor_holdings_log": ["기준일", "계좌", "종목코드"],
    # 미국장
    "global_ticker": ["Symbol", "country", "date"],
    "global_price": ["Date", "Symbol"],
    "global_fs": ["Symbol", "date", "account", "freq"],
    "global_value": ["Symbol", "date", "지표"],
}

KOR_TABLES = [t for t in PRIMARY_KEYS if t.startswith("kor_")]
US_TABLES = [t for t in PRIMARY_KEYS if t.startswith("global_")]

# 파일이 없을 때 만들어 둘 빈 테이블의 컬럼 (타입 포함).
# 예전에는 dummy 컬럼 하나짜리 빈 테이블을 만들었는데, 그러면 실제 컬럼을 쓰는
# 쿼리가 "Referenced column 공시구분 not found" 같은 엉뚱한 오류로 죽었습니다.
# 스키마를 갖춘 빈 테이블이면 0건이 깔끔하게 반환됩니다.
SCHEMAS: dict[str, dict[str, str]] = {
    "kor_ticker": {"종목코드": "VARCHAR", "종목명": "VARCHAR", "시장구분": "VARCHAR",
                   "종가": "DOUBLE", "시가총액": "DOUBLE", "기준일": "TIMESTAMP",
                   "EPS": "DOUBLE", "BPS": "DOUBLE", "주당배당금": "DOUBLE",
                   "종목구분": "VARCHAR"},
    "kor_sector": {"IDX_CD": "VARCHAR", "CMP_CD": "VARCHAR", "CMP_KOR": "VARCHAR",
                   "SEC_NM_KOR": "VARCHAR", "기준일": "TIMESTAMP"},
    "kor_price": {"날짜": "TIMESTAMP", "시가": "DOUBLE", "고가": "DOUBLE",
                  "저가": "DOUBLE", "종가": "DOUBLE", "거래량": "DOUBLE",
                  "종목코드": "VARCHAR"},
    "kor_fs": {"계정": "VARCHAR", "기준일": "TIMESTAMP", "값": "DOUBLE",
               "종목코드": "VARCHAR", "공시구분": "VARCHAR",
               "출처": "VARCHAR"},
    "kor_value": {"종목코드": "VARCHAR", "기준일": "TIMESTAMP", "지표": "VARCHAR",
                  "값": "DOUBLE"},
    # 기준금액 = 그 종목을 마지막으로 손봤을 때의 목표 금액. 실시간 평가액이
    # 아니라 '그때 맞춰 둔 값'입니다. 비중 조절 수량을 계산하는 데 씁니다.
    # 운용자금 — 월 1회 재동기화로 갱신. 한 줄만 유지합니다.
    "kor_capital": {"기준일": "TIMESTAMP", "평가액": "DOUBLE", "현금": "DOUBLE",
                    "총자본": "DOUBLE"},
    # 두 번째 프로필(alt) — 같은 스키마, 별도 테이블. 상태가 섞이면
    # 한 사람의 매도가 다른 사람 주문서에 나타납니다.
    "kor_capital_alt": {"기준일": "TIMESTAMP", "평가액": "DOUBLE", "현금": "DOUBLE",
                        "총자본": "DOUBLE"},
    # 주문서가 낸 모든 주문. 매주 덮어쓰지 않고 쌓습니다.
    #
    # 왜 필요한가: 상태 테이블(kor_portfolio)은 '지금 들고 있는 것'만 담습니다.
    # 팔린 종목은 통째로 사라지므로 실현손익을 계산할 방법이 없고, 주문서
    # 엑셀과 weekly_*.json은 매주 같은 이름으로 덮어써집니다. 즉 기록을
    # 남기지 않으면 그 주의 거래는 영구히 사라집니다.
    #
    # '주가·수량·예상금액'은 주문서가 계산한 **계획**입니다. 실제 체결가는
    # 다를 수 있어 체결가/체결수량/체결일을 따로 둡니다(비면 계획값으로
    # 추정한다는 뜻).
    "kor_trades": {"기준일": "TIMESTAMP", "계좌": "VARCHAR", "구분": "VARCHAR",
                   "종목코드": "VARCHAR", "종목명": "VARCHAR", "섹터": "VARCHAR",
                   "주가": "DOUBLE", "수량": "DOUBLE", "예상금액": "DOUBLE",
                   "등분": "BIGINT", "메모": "VARCHAR",
                   "체결가": "DOUBLE", "체결수량": "DOUBLE",
                   "체결일": "TIMESTAMP"},
    # 재동기화 때의 종목별 평가금액 — 실제 계좌와 맞춰 본 유일한 지점입니다.
    # 수량을 모르는 종목(이어받은 보유)의 주식 수를 평가금액÷종가로
    # 되살리는 기준점이 되고, 계획과 실제가 얼마나 벌어졌는지도 여기서 봅니다.
    "kor_holdings_log": {"기준일": "TIMESTAMP", "계좌": "VARCHAR",
                         "종목코드": "VARCHAR", "종목명": "VARCHAR",
                         "평가금액": "DOUBLE", "수량": "DOUBLE"},
    # 수량은 주문 계산에 쓰지 않습니다(금액으로 돕니다). 일별 평가액과
    # 주식수 교차검증용으로 재동기화 때 받아 둡니다 — 없으면 비어 있습니다.
    "kor_portfolio": {"종목코드": "VARCHAR", "등분": "BIGINT", "편입일": "TIMESTAMP",
                      "기준금액": "DOUBLE", "수량": "DOUBLE",
                      "종목명": "VARCHAR", "섹터": "VARCHAR"},
    "global_ticker": {"Name": "VARCHAR", "Symbol": "VARCHAR", "Exchange": "VARCHAR",
                      "Sector": "VARCHAR", "Market Cap": "DOUBLE",
                      "Dividend": "DOUBLE", "country": "VARCHAR", "date": "TIMESTAMP"},
    "global_price": {"Date": "TIMESTAMP", "High": "DOUBLE", "Low": "DOUBLE",
                     "Open": "DOUBLE", "Close": "DOUBLE", "Volume": "DOUBLE",
                     "Symbol": "VARCHAR"},
    "global_fs": {"Symbol": "VARCHAR", "date": "TIMESTAMP", "account": "VARCHAR",
                  "value": "DOUBLE", "freq": "VARCHAR"},
    "global_value": {"Symbol": "VARCHAR", "date": "TIMESTAMP", "지표": "VARCHAR",
                     "값": "DOUBLE"},
}

# 날짜로 취급할 컬럼 (parquet 왕복 시 타입 고정)
# alt 프로필의 보유 테이블은 main과 스키마가 같습니다 (복제해 등록).
SCHEMAS["kor_portfolio_alt"] = dict(SCHEMAS["kor_portfolio"])

DATE_COLS = {"기준일", "날짜", "date", "Date"}

_write_lock = threading.Lock()


def path(table: str):
    return config.DATA_DIR / f"{table}.parquet"


def exists(table: str) -> bool:
    return path(table).exists()


def _normalize(df: pd.DataFrame, table: str) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if col in DATE_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    # 한국 종목코드는 항상 6자리 문자열 (미국 Symbol은 문자라 건드리지 않음)
    for col in ("종목코드", "CMP_CD"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.zfill(6)
    if "Symbol" in df.columns:
        df["Symbol"] = df["Symbol"].astype(str).str.strip()
    return df


def read(table: str) -> pd.DataFrame:
    """테이블 전체를 DataFrame으로. 없으면 빈 DataFrame."""
    if not exists(table):
        return pd.DataFrame()
    return pd.read_parquet(path(table))


def write(table: str, df: pd.DataFrame) -> int:
    """테이블 전체를 덮어씁니다."""
    df = _normalize(df, table)
    with _write_lock:
        tmp = path(table).with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path(table))
    return len(df)


def upsert(table: str, df: pd.DataFrame) -> int:
    """기본키가 겹치면 새 값으로 교체, 없으면 추가. 반영된 행 수를 반환."""
    if df is None or len(df) == 0:
        return 0

    keys = PRIMARY_KEYS.get(table)
    if not keys:
        raise ValueError(f"알 수 없는 테이블: {table}")

    new = _normalize(df, table)
    n_new = len(new)

    with _write_lock:
        old = pd.read_parquet(path(table)) if path(table).exists() else pd.DataFrame()
        if len(old):
            # 컬럼 순서를 기존 테이블에 맞춤
            new = new.reindex(columns=[c for c in old.columns if c in new.columns]
                              + [c for c in new.columns if c not in old.columns])
            merged = pd.concat([old, new], ignore_index=True)
        else:
            merged = new

        merged = merged.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        merged = merged.sort_values(keys).reset_index(drop=True)

        tmp = path(table).with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path(table))

    return n_new


PRICE_DATE_COL = {"kor_price": "날짜", "global_price": "Date"}


def prune_price(table: str = "kor_price", keep_years: int | None = None) -> int:
    """주가 테이블에서 오래된 행을 잘라 저장소 크기를 제한합니다. 남은 행 수 반환."""
    keep_years = keep_years or config.PRICE_KEEP_YEARS
    col = PRICE_DATE_COL[table]
    if not exists(table):
        return 0
    df = pd.read_parquet(path(table))
    if df.empty:
        return 0
    cutoff = df[col].max() - pd.DateOffset(years=keep_years)
    kept = df[df[col] >= cutoff]
    if len(kept) < len(df):
        write(table, kept)
    return len(kept)


def connect() -> duckdb.DuckDBPyConnection:
    """존재하는 모든 테이블을 뷰로 등록한 DuckDB 커넥션."""
    con = duckdb.connect()
    for table in PRIMARY_KEYS:
        p = path(table)
        if p.exists():
            con.execute(
                f"create view {table} as select * from read_parquet('{p.as_posix()}')"
            )
        else:
            # 파일이 없어도 쿼리가 깨지지 않도록 스키마를 갖춘 빈 테이블을 만듭니다.
            cols = ", ".join(f'"{c}" {t}' for c, t in SCHEMAS[table].items())
            con.execute(f"create table {table} ({cols})")
    return con


def read_sql(query: str) -> pd.DataFrame:
    """기존 MySQL 쿼리를 그대로 실행 (DuckDB 문법 호환)."""
    con = connect()
    try:
        return con.execute(query).df()
    finally:
        con.close()


def common_tickers(limit: int | None = None) -> list[str]:
    """최신 기준일의 보통주 종목코드 목록."""
    if not exists("kor_ticker"):
        return []
    df = read_sql("""
        select 종목코드 from kor_ticker
        where 기준일 = (select max(기준일) from kor_ticker)
          and 종목구분 = '보통주'
        order by 종목코드;
    """)
    tickers = df["종목코드"].astype(str).str.zfill(6).tolist()
    limit = limit if limit is not None else config.TICKER_LIMIT
    if limit:
        tickers = tickers[:limit]
    return tickers


def us_symbols(limit: int | None = None) -> list[str]:
    """최신 date의 미국 상장 종목 심볼 목록."""
    if not exists("global_ticker"):
        return []
    df = read_sql("""
        select Symbol from global_ticker
        where date = (select max(date) from global_ticker)
          and country = 'United States'
        order by "Market Cap" desc nulls last;
    """)
    symbols = df["Symbol"].astype(str).str.strip().tolist()
    limit = limit if limit is not None else config.TICKER_LIMIT
    if limit:
        symbols = symbols[:limit]
    return symbols


def summary(tables=None) -> dict:
    """저장소 현황 — 대시보드/리포트용."""
    out = {}
    for table in (tables or PRIMARY_KEYS):
        p = path(table)
        if not p.exists():
            out[table] = {"rows": 0, "mb": 0.0, "latest": None}
            continue
        df = pd.read_parquet(p)
        date_col = next((c for c in ("기준일", "날짜", "date", "Date")
                         if c in df.columns), None)
        latest = str(df[date_col].max().date()) if date_col and len(df) else None
        out[table] = {
            "rows": len(df),
            "mb": round(p.stat().st_size / 1024 / 1024, 2),
            "latest": latest,
        }
    return out


def init_db(market: str | None = None) -> dict:
    """MySQL 시절 DDL 단계 대체 — 폴더를 준비하고 빠진 테이블을 보고합니다."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    wanted = {"kr": KOR_TABLES, "us": US_TABLES}.get(market, list(PRIMARY_KEYS))
    present = [t for t in wanted if exists(t)]
    missing = [t for t in wanted if not exists(t)]

    out = {"data_dir": str(config.DATA_DIR), "tables": present}
    if missing:
        # 이게 비어 있으면 뒤 단계가 0건으로 돌다가 엉뚱한 곳에서 터집니다.
        out["missing"] = missing
    return out
