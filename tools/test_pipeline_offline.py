# -*- coding: utf-8 -*-
"""네트워크 없이 파이프라인 검증.

합성 데이터를 저장소에 넣고 kor_value → factor_kor 를 끝까지 돌려
store.py(DuckDB/Parquet), SQL 호환성, 팩터 계산, 엑셀 출력을 확인합니다.
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

TMP = Path(tempfile.mkdtemp(prefix="quant_test_"))
os.environ["QUANT_DATA_DIR"] = str(TMP / "data")
os.environ["QUANT_OUTPUT_DIR"] = str(TMP / "output")
os.environ["QUANT_LOG_DIR"] = str(TMP / "logs")
os.environ["QUANT_N_PORTFOLIO"] = "120"

import config  # noqa: E402
import factor_kor  # noqa: E402
import kor_value  # noqa: E402
import store  # noqa: E402

rng = np.random.default_rng(7)
N_STOCK = 260
BIZ_DAY = pd.Timestamp("2026-09-11")
SECTORS = ["반도체", "건강관리", "소프트웨어", "금융", "산업재",
           "소재", "에너지", "필수소비재", "경기소비재", "커뮤니케이션"]

codes = [f"{i * 10:06d}" for i in range(1, N_STOCK + 1)]
# 우선주(끝자리가 0이 아님) 10개를 섞어 필터링 동작 확인 — 코드는 모두 유일해야 함
pref_codes = [f"{900000 + i * 10 + 5:06d}" for i in range(10)]
assert len(set(codes + pref_codes)) == N_STOCK + 10, "테스트 종목코드 중복"

fails = []


def check(cond, msg):
    print(f"[{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


# ── 1. kor_ticker ────────────────────────────────────────────
ticker = pd.DataFrame({
    "종목코드": codes + pref_codes,
    "종목명": [f"테스트{i}" for i in range(len(codes))] + [f"우선{i}" for i in range(10)],
    "시장구분": ["KOSPI"] * (len(codes) + 10),
    "종가": rng.uniform(1000, 90000, len(codes) + 10).round(0),
    "시가총액": rng.uniform(5e10, 9e12, len(codes) + 10),
    "기준일": BIZ_DAY,
    "EPS": rng.uniform(-500, 9000, len(codes) + 10),
    "BPS": rng.uniform(1000, 80000, len(codes) + 10),
    "주당배당금": rng.choice([0, 200, 500, 1200], len(codes) + 10),
    "종목구분": ["보통주"] * len(codes) + ["우선주"] * 10,
})
store.upsert("kor_ticker", ticker)
check(store.exists("kor_ticker"), "kor_ticker parquet 생성")

got = store.common_tickers()
check(len(got) == N_STOCK, f"보통주 필터 {len(got)}개 == {N_STOCK}개")

# 업서트 멱등성: 같은 키로 다시 넣어도 행이 늘지 않아야 함
ticker2 = ticker.copy()
ticker2["종가"] = ticker2["종가"] * 2
store.upsert("kor_ticker", ticker2)
t = store.read("kor_ticker")
check(len(t) == N_STOCK + 10, f"업서트 멱등성 — 행 수 유지 ({len(t)} == {N_STOCK + 10})")
check(float(t.iloc[0]["종가"]) == float(ticker2.sort_values(['종목코드','기준일']).iloc[0]["종가"]),
      "업서트 시 최신 값으로 교체됨")

# ── 2. kor_sector ────────────────────────────────────────────
sector = pd.DataFrame({
    "IDX_CD": ["G10"] * N_STOCK,
    "CMP_CD": codes,
    "CMP_KOR": [f"테스트{i}" for i in range(N_STOCK)],
    "SEC_NM_KOR": [SECTORS[i % len(SECTORS)] for i in range(N_STOCK)],
    "기준일": BIZ_DAY,
})
store.upsert("kor_sector", sector)

# ── 3. kor_price (14개월치 영업일) ───────────────────────────
days = pd.bdate_range(end=BIZ_DAY, periods=300)
frames = []
for c in codes:
    px = 10000 * np.exp(np.cumsum(rng.normal(0.0004, 0.022, len(days))))
    frames.append(pd.DataFrame({
        "날짜": days, "시가": px * 0.99, "고가": px * 1.02, "저가": px * 0.98,
        "종가": px, "거래량": rng.integers(1e4, 1e7, len(days)), "종목코드": c,
    }))
store.upsert("kor_price", pd.concat(frames, ignore_index=True))
check(store.read("kor_price").shape[0] == N_STOCK * len(days),
      f"kor_price {N_STOCK * len(days):,}행 저장")

# interval 쿼리(최근 1년) 동작 확인
recent = store.read_sql("""
    select 날짜, 종가, 종목코드 from kor_price
    where 날짜 >= (select (select max(날짜) from kor_price) - interval 1 year);
""")
n_days_1y = recent["날짜"].nunique()
check(200 < n_days_1y < 270, f"interval 1 year 쿼리 → {n_days_1y} 영업일")

# 보관 기간 정리
os.environ["QUANT_PRICE_KEEP_YEARS"] = "1"
import importlib  # noqa: E402
importlib.reload(config)
importlib.reload(store)
kept = store.prune_price(keep_years=1)
check(kept < N_STOCK * len(days), f"prune_price 동작 — {kept:,}행 남김")

# ── 4. kor_fs (분기 재무제표 12분기) ─────────────────────────
quarters = pd.date_range(end="2026-06-30", periods=12, freq="QE")
accounts = ["당기순이익", "자본", "영업활동으로인한현금흐름", "매출액", "매출총이익", "자산"]
fs_rows = []
for c in codes:
    scale = rng.uniform(50, 5000)
    for acct in accounts:
        base = scale * {"당기순이익": 1.0, "자본": 12.0, "영업활동으로인한현금흐름": 1.4,
                        "매출액": 9.0, "매출총이익": 2.5, "자산": 20.0}[acct]
        vals = base * (1 + rng.normal(0, 0.12, len(quarters)))
        fs_rows.append(pd.DataFrame({"계정": acct, "기준일": quarters, "값": vals,
                                     "종목코드": c, "공시구분": "q"}))
store.upsert("kor_fs", pd.concat(fs_rows, ignore_index=True))
check(store.read("kor_fs").shape[0] == N_STOCK * len(accounts) * len(quarters),
      "kor_fs 저장")

# ── 5. 밸류 지표 ─────────────────────────────────────────────
vres = kor_value.build()
print("    kor_value.build() →", vres)
value = store.read("kor_value")
check(vres["value_rows"] > 0, "밸류 지표 계산")
check(set(value["지표"].unique()) >= {"PER", "PBR", "PCR", "PSR"},
      f"지표 종류 {sorted(value['지표'].unique())}")

# ── 6. 팩터 모델 ─────────────────────────────────────────────
res = factor_kor.run()
print("    factor_kor.run() →", {k: v for k, v in res.items() if k != "top_buys"})
# 원본 알고리즘은 섹터별 상하위 1%를 NaN으로 잘라내므로 qvm이 NaN인 종목이 생기고,
# 선정 종목 수는 N_PORTFOLIO 이하가 됩니다 (실제 데이터에서도 동일한 동작).
check(0 < res["selected"] <= 120, f"포트폴리오 선정 {res['selected']}개 (상한 120)")

xlsx = Path(res["excel"])
check(xlsx.exists() and xlsx.stat().st_size > 5000, f"엑셀 생성 {xlsx.name}")

model = pd.read_parquet(config.OUTPUT_DIR / "model_kr_latest.parquet")
need = {"종목코드", "종목명", "SEC_NM_KOR", "qvm",
        "z_quality", "z_value", "z_momentum",
        "ROE", "GPA", "CFO", "PER", "PBR", "12M", "K_ratio",
        "MACD", "RSI", "BB", "매수/매도"}
missing = need - set(model.columns)
check(not missing, f"모델 컬럼 확인 (총 {len(model.columns)}개, 누락 {missing or '없음'})")
check(model["qvm"].notna().sum() > 100, "qvm 점수 계산됨")
check(model["RSI"].notna().sum() > 100, "RSI 계산됨")
check(model["qvm"].is_monotonic_increasing, "qvm 오름차순 정렬(우수 종목 상단)")

labels = model["매수/매도"].value_counts().to_dict()
print("    매수/매도 분포:", labels)
check(any("매수" in str(k) or "매도" in str(k) for k in labels),
      "기술적 매매 신호 생성됨")
check(res["buy_signals"] + res["sell_signals"] > 0,
      f"매수 {res['buy_signals']} / 매도 {res['sell_signals']} 신호")

# ── 7. 섹터 중립화 (전체 유니버스 기준) ─────────────────────
# 모델 결과는 qvm 상위만 남긴 것이라 섹터 평균이 0일 수 없습니다.
# 중립성은 선정 전 전체 종목에 대해 확인해야 합니다.
import factor_core as fc  # noqa: E402

rng2 = np.random.default_rng(11)
universe = pd.DataFrame({
    "Symbol": [f"U{i:04d}" for i in range(400)],
    "Sector": [SECTORS[i % len(SECTORS)] for i in range(400)],
    "ROE": rng2.normal(0.1, 0.2, 400), "GPA": rng2.normal(0.3, 0.2, 400),
    "CFO": rng2.normal(0.1, 0.1, 400), "PER": rng2.lognormal(2.4, .6, 400),
    "PBR": rng2.lognormal(0.3, .5, 400), "PCR": rng2.lognormal(2, .6, 400),
    "PSR": rng2.lognormal(1, .7, 400), "DY": rng2.choice([np.nan, 0.01, 0.03], 400),
    "12M": rng2.normal(.08, .3, 400), "K_ratio": rng2.normal(2, 4, 400),
})
scored = fc.build_scores(universe, symbol="Symbol", sector="Sector",
                         weights=[1/3, 1/3, 1/3], n_portfolio=150)
sec_mean = scored.groupby("Sector")["z_quality"].mean().abs().max()
check(sec_mean < 0.05, f"섹터 중립화 — 섹터별 z_quality 평균 절대값 최대 {sec_mean:.4f}")
check(scored["qvm"].notna().sum() == len(scored),
      f"윈저라이즈 — 전 종목이 qvm 점수를 받음 ({scored['qvm'].notna().sum()}/{len(scored)})")
check(int((scored["invest"] == "Y").sum()) == 150,
      f"포트폴리오 정확히 150개 선정 ({int((scored['invest'] == 'Y').sum())})")

# DY 결측이 밸류 팩터를 통째로 날리지 않는지
no_dy = scored[universe["DY"].isna().values]
check(no_dy["z_value"].notna().all(),
      f"무배당 종목도 밸류 점수 유지 ({no_dy['z_value'].notna().sum()}/{len(no_dy)})")

print(f"\n저장소 현황: {store.summary()}")
shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패한 항목:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — MySQL 없이 파이프라인이 끝까지 동작합니다.")
