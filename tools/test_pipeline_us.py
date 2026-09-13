# -*- coding: utf-8 -*-
"""미국장 파이프라인을 네트워크 없이 검증.

yfinance / yahooquery / nasdaq API 응답을 합성해 넣고
변환 → 저장 → 밸류 → 팩터까지 끝까지 돌려봅니다.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="quant_us_test_"))
os.environ["QUANT_DATA_DIR"] = str(TMP / "data")
os.environ["QUANT_OUTPUT_DIR"] = str(TMP / "output")
os.environ["QUANT_LOG_DIR"] = str(TMP / "logs")
os.environ["QUANT_N_PORTFOLIO"] = "200"

import config  # noqa: E402
import factor_global  # noqa: E402
import global_price  # noqa: E402
import global_ticker  # noqa: E402
import global_value  # noqa: E402
import store  # noqa: E402

rng = np.random.default_rng(3)
N = 500
AS_OF = pd.Timestamp("2026-09-11")
SECTORS = ["Technology", "Health Care", "Finance", "Energy", "Utilities",
           "Consumer Discretionary", "Industrials", "Basic Materials",
           "Real Estate", "Telecommunications"]

fails = []


def check(cond, msg):
    print(f"[{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


def sym(i):
    a = chr(65 + i // 676)
    b = chr(65 + (i // 26) % 26)
    c = chr(65 + i % 26)
    return f"{a}{b}{c}"


symbols = [sym(i) for i in range(N)]

# ── 1. 나스닥 스크리너 응답 정리 ─────────────────────────────
raw = pd.DataFrame({
    "Name": [f"{s} Corp" for s in symbols] + ["Bad Warrant", "Dollar Co", "Zero Cap"],
    "Symbol": symbols + ["ABC.WS", "$XYZ", "NUL"],
    "Sector": [SECTORS[i % len(SECTORS)] for i in range(N)] + ["", "Technology", "Energy"],
    # 문자 섞인 시총 표기도 들어옵니다
    "Market Cap": ([f"${v:,.2f}" for v in rng.uniform(5e8, 3e12, N)]
                   + ["1000000", "2000000", "0"]),
    "country": ["United States"] * N + ["United States"] * 3,
    "Exchange": ["NASDAQ"] * N + ["NYSE"] * 3,
})
clean = global_ticker._clean(raw, "20260911")

check(len(clean) == N, f"비정상 종목 제외 ({len(clean)} == {N}) — 워런트/$·시총0 걸러짐")
check("ABC.WS" not in set(clean["Symbol"]), "워런트(.WS) 제외")
check("$XYZ" not in set(clean["Symbol"]), "$ 접두 종목 제외")
check(clean["Market Cap"].dtype.kind == "f", "시총 문자열 → 숫자 변환")
check(clean["Market Cap"].max() > 1e11, f"대형주 시총 보존 (최대 {clean['Market Cap'].max():.3g})")
store.upsert("global_ticker", clean)
check(len(store.us_symbols()) == N, f"us_symbols() {len(store.us_symbols())}개")

# 시총 내림차순인지 (TICKER_LIMIT을 걸면 대형주부터 잡혀야 함)
head = store.us_symbols(limit=5)
mc = clean.set_index("Symbol")["Market Cap"]
check(list(mc[head]) == sorted(mc[head], reverse=True), "us_symbols가 시총 내림차순")

# ── 2. 스크리너 CSV 대체 경로 ────────────────────────────────
csv_dir = config.DATA_DIR
pd.DataFrame({"Name": ["Apple Inc"], "Symbol": ["AAPL"], "Sector": ["Technology"],
              "Market Cap": [3.5e12], "Country": ["United States"]}) \
    .to_csv(csv_dir / "nasdaq_screener_NASDAQ.csv", index=False)
alt = global_ticker._from_csv()
check(len(alt) == 1 and alt.iloc[0]["Symbol"] == "AAPL" and alt.iloc[0]["Exchange"] == "NASDAQ",
      "CSV 대체 경로 — 파일명에서 거래소 인식")
(csv_dir / "nasdaq_screener_NASDAQ.csv").unlink()

# ── 3. yfinance 응답 변환 (가장 깨지기 쉬운 부분) ────────────
days = pd.bdate_range(end=AS_OF, periods=300)
chunk = symbols[:4]
cols = pd.MultiIndex.from_product([chunk, ["Open", "High", "Low", "Close", "Volume"]])
yf_like = pd.DataFrame(index=pd.DatetimeIndex(days, name="Date"),
                       columns=cols, dtype="float64")
for s in chunk:
    px = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, len(days))))
    yf_like[(s, "Open")] = px * 0.99
    yf_like[(s, "High")] = px * 1.02
    yf_like[(s, "Low")] = px * 0.98
    yf_like[(s, "Close")] = px
    yf_like[(s, "Volume")] = rng.integers(1e5, 1e8, len(days))

long = global_price._to_long(yf_like, chunk)
check(list(long.columns) == global_price.COLUMNS, f"컬럼 순서 {list(long.columns)}")
check(len(long) == len(days) * len(chunk), f"행 수 {len(long)} == {len(days) * len(chunk)}")
check(set(long["Symbol"]) == set(chunk), "심볼 4개 모두 복원")
check(long["Close"].notna().all(), "종가 결측 없음")
one = long[long["Symbol"] == chunk[0]].sort_values("Date")
check(np.isclose(one["Close"].iloc[-1], yf_like[(chunk[0], "Close")].iloc[-1]),
      "마지막 종가가 원본과 일치")

# 단일 종목(평평한 컬럼) 응답도 처리되는지
flat = pd.DataFrame({"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5],
                     "Volume": [100.0]}, index=pd.DatetimeIndex([AS_OF], name="Date"))
one_long = global_price._to_long(flat, ["SOLO"])
check(len(one_long) == 1 and one_long.iloc[0]["Symbol"] == "SOLO",
      "단일 종목 응답(평평한 컬럼)도 변환")

# 전 종목 주가 적재
frames = []
for s in symbols:
    px = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.022, len(days))))
    frames.append(pd.DataFrame({"Date": days, "High": px * 1.02, "Low": px * .98,
                                "Open": px * .99, "Close": px,
                                "Volume": rng.integers(1e5, 1e8, len(days)),
                                "Symbol": s}))
store.upsert("global_price", pd.concat(frames, ignore_index=True))
check(store.read("global_price").shape[0] == N * len(days),
      f"global_price {N * len(days):,}행")

recent = store.read_sql("""
    select Date, Close, Symbol from global_price
    where Date >= (select (select max(Date) from global_price) - interval 1 year);
""")
check(200 < recent["Date"].nunique() < 270,
      f"interval 1 year 쿼리 → {recent['Date'].nunique()} 거래일")

# ── 4. 재무제표 ──────────────────────────────────────────────
quarters = pd.date_range(end="2026-06-30", periods=12, freq="QE")
accounts = {"NetIncome": 1.0, "StockholdersEquity": 12.0,
            "CashFlowFromContinuingOperatingActivities": 1.4,
            "TotalRevenue": 9.0, "GrossProfit": 2.5, "TotalAssets": 20.0}
rows = []
for s in symbols:
    scale = rng.uniform(1e7, 5e9)
    for acct, mult in accounts.items():
        rows.append(pd.DataFrame({"Symbol": s, "date": quarters,
                                  "account": acct,
                                  "value": scale * mult * (1 + rng.normal(0, .12, 12)),
                                  "freq": "q"}))
store.upsert("global_fs", pd.concat(rows, ignore_index=True))
check(store.read("global_fs").shape[0] == N * len(accounts) * 12, "global_fs 저장")

# ── 5. 밸류 지표 (배당은 네트워크 대신 합성) ─────────────────
fake_dy = pd.Series({s: round(float(v), 4) for s, v in
                     zip(symbols, rng.choice([0, 0, 1.2, 2.8, 4.1], N))
                     if v > 0}, dtype="float64")
global_value._dividend_yield = lambda syms: fake_dy   # 네트워크 차단

vres = global_value.build()
print("    global_value.build() →", vres)
value = store.read("global_value")
check(vres["value_rows"] > 0, "밸류 지표 계산")
check(set(value["지표"].unique()) == {"PER", "PBR", "PCR", "PSR", "DY"},
      f"지표 종류 {sorted(value['지표'].unique())}")
check(vres["dy_rows"] == len(fake_dy), f"배당 {vres['dy_rows']}종목 저장")

# ── 6. 팩터 모델 ─────────────────────────────────────────────
res = factor_global.run()
print("    factor_global.run() →", {k: v for k, v in res.items() if k != "top_buys"})
check(res["selected"] == 200, f"포트폴리오 선정 {res['selected']}개 (목표 200)")
check(res["universe"] == N, f"유니버스 {res['universe']}개")

xlsx = Path(res["excel"])
check(xlsx.exists() and xlsx.stat().st_size > 5000, f"엑셀 생성 {xlsx.name}")

model = pd.read_parquet(config.OUTPUT_DIR / "model_us_latest.parquet")
need = {"Symbol", "Name", "Sector", "qvm", "z_quality", "z_value", "z_momentum",
        "ROE", "GPA", "CFO", "PER", "PBR", "PSR", "PCR", "DY", "12M", "K_ratio"}
missing = need - set(model.columns)
check(not missing, f"모델 컬럼 확인 (총 {len(model.columns)}개, 누락 {missing or '없음'})")
check("매수/매도" not in model.columns, "기술적 신호 없음 (노트북과 동일)")
check(model["qvm"].is_monotonic_increasing, "qvm 오름차순 정렬")
check(model["qvm"].notna().all(), "선정 종목 전부 qvm 보유")

print(f"\n저장소 현황: {store.summary(store.US_TABLES)}")
shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패한 항목:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 미국장 파이프라인이 끝까지 동작합니다.")
