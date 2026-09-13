# -*- coding: utf-8 -*-
"""백테스트 엔진 검증.

핵심은 두 가지입니다.
  1. 정답을 심어둔 합성 데이터에서 그 정답을 찾아내는가
     (좋은 종목이 실제로 더 오르도록 만들어 두고, 모델이 그걸 골라
      벤치마크를 이기는지 본다. 못 이기면 엔진이 잘못된 것이다)
  2. 미래를 훔쳐보지 않는가
     (아직 공시되지 않은 재무제표를 끼워 넣어도 점수가 변하지 않아야 한다)
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

TMP = Path(tempfile.mkdtemp(prefix="quant_bt_"))
os.environ["QUANT_DATA_DIR"] = str(TMP / "data")
os.environ["QUANT_OUTPUT_DIR"] = str(TMP / "output")
os.environ["QUANT_LOG_DIR"] = str(TMP / "logs")

import backtest  # noqa: E402
import store  # noqa: E402

rng = np.random.default_rng(20260913)
N = 300
DAYS = pd.bdate_range(end="2026-09-11", periods=1000)   # 약 4년
SECTORS = ["반도체", "건강관리", "소프트웨어", "금융", "산업재",
           "소재", "에너지", "필수소비재", "경기소비재", "커뮤니케이션"]

fails = []


def check(cond, msg):
    print(f"[{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


codes = [f"{(i + 1) * 10:06d}" for i in range(N)]

# ── 정답 심기: alpha가 높은 종목일수록 더 오르고 펀더멘털도 좋게 ──
alpha = np.linspace(-0.00055, 0.00055, N)
rng.shuffle(alpha)
quality_rank = pd.Series(alpha, index=codes)   # 이게 '정답'

frames, last_close = [], {}
for i, c in enumerate(codes):
    px = 20000 * np.exp(np.cumsum(rng.normal(alpha[i], 0.018, len(DAYS))))
    last_close[c] = px[-1]
    frames.append(pd.DataFrame({"날짜": DAYS, "시가": px * .99, "고가": px * 1.02,
                                "저가": px * .98, "종가": px,
                                "거래량": rng.integers(1e4, 1e7, len(DAYS)),
                                "종목코드": c}))
store.upsert("kor_price", pd.concat(frames, ignore_index=True))

shares_out = rng.uniform(5e6, 5e8, N)
ticker = pd.DataFrame({
    "종목코드": codes,
    "종목명": [f"테스트{i}" for i in range(N)],
    "시장구분": "KOSPI",
    "종가": [last_close[c] for c in codes],
    "시가총액": [last_close[c] * s for c, s in zip(codes, shares_out)],
    "기준일": DAYS[-1], "EPS": 0.0, "BPS": 0.0,
    "주당배당금": rng.choice([0, 300, 800], N).astype(float),
    "종목구분": "보통주",
})
store.upsert("kor_ticker", ticker)
store.upsert("kor_sector", pd.DataFrame({
    "IDX_CD": "G10", "CMP_CD": codes,
    "CMP_KOR": [f"테스트{i}" for i in range(N)],
    "SEC_NM_KOR": [SECTORS[i % len(SECTORS)] for i in range(N)],
    "기준일": DAYS[-1]}))

# 분기 재무제표 — 수익성이 alpha와 같은 방향이 되도록
quarters = pd.date_range(end="2026-06-30", periods=20, freq="QE")
rows = []
for i, c in enumerate(codes):
    scale = rng.uniform(80, 4000)
    tilt = 1 + (alpha[i] / 0.00055) * 0.45          # 좋은 종목일수록 이익률 ↑
    for acct, mult in [("당기순이익", 1.0 * tilt), ("자본", 12.0),
                       ("영업활동으로인한현금흐름", 1.4 * tilt),
                       ("매출액", 9.0), ("매출총이익", 2.5 * tilt),
                       ("자산", 20.0)]:
        rows.append(pd.DataFrame({
            "계정": acct, "기준일": quarters, "값": scale * mult * (1 + rng.normal(0, .08, 20)),
            "종목코드": c, "공시구분": "q"}))
store.upsert("kor_fs", pd.concat(rows, ignore_index=True))

print(f"합성 데이터: {N}종목 · {len(DAYS)}거래일 · {len(quarters)}분기\n")

# ── 1. 미래 훔쳐보기 차단 ────────────────────────────────────
spec = backtest.SPECS["kr"]
price, fs, tk, sector = backtest._load(spec)
price["날짜"] = pd.to_datetime(price["날짜"])
fs["기준일"] = pd.to_datetime(fs["기준일"])
pivot = price.pivot_table(index="날짜", columns="종목코드",
                          values="종가", aggfunc="last").sort_index()
shares = backtest._shares(tk, spec)

asof = pivot.index[-1]
clean = backtest._score_at(asof, pivot, fs, tk, sector, shares, spec, 90)
check(clean is not None and len(clean) > 50,
      f"점수 산출 동작 ({len(clean) if clean is not None else 0}종목)")

# 아직 공시되지 않았어야 할 시점(asof-30일)에 말도 안 되는 값을 끼워 넣는다
poison = pd.DataFrame({
    "계정": "당기순이익", "기준일": asof - pd.Timedelta(days=30),
    "값": 1e12, "종목코드": codes[:50], "공시구분": "q"})
dirty = backtest._score_at(asof, pivot, pd.concat([fs, poison], ignore_index=True),
                           tk, sector, shares, spec, 90)
same = (clean.set_index("symbol")["qvm"].round(8)
        .equals(dirty.set_index("symbol")["qvm"].round(8)))
check(same, "공시 전 재무제표는 무시됨 (미래 훔쳐보기 차단)")

# 공시지연을 0으로 하면 그 값이 반영되어야 한다 — 차단이 '진짜로' 동작하는지 반증
leaked = backtest._score_at(asof, pivot, pd.concat([fs, poison], ignore_index=True),
                            tk, sector, shares, spec, 0)
changed = not (clean.set_index("symbol")["qvm"].round(8)
               .equals(leaked.set_index("symbol")["qvm"].round(8)))
check(changed, "공시지연 0일이면 반영됨 (차단 로직이 실제로 작동한다는 반증)")

# ── 2. 심어둔 정답을 찾아내는가 ──────────────────────────────
res = backtest.run(market="kr", top_n=30, rebalance="QE", cost_bps=25)
s, perf = res["summary"], res["perf"]
p, b = s["포트폴리오"], s["벤치마크(전종목 동일가중)"]

print(f"\n    기간 {s['기간']} · 리밸런싱 {s['리밸런싱횟수']}회")
print(f"    포트폴리오 누적 {p['누적수익률']}% / 벤치마크 {b['누적수익률']}% "
      f"→ 초과 {s['초과수익률']}%p")
print(f"    MDD {p['최대낙폭']}% · 승률 {p['승률']}% · 턴오버 {s['평균턴오버']}%\n")

check(s["리밸런싱횟수"] >= 8, f"리밸런싱 {s['리밸런싱횟수']}회 수행")
check(s["초과수익률"] > 0,
      f"심어둔 정답을 찾아냄 — 벤치마크 대비 +{s['초과수익률']}%p")

# 선정 종목의 실제 alpha가 전체 평균보다 높아야 한다
picked = set(res["holdings"]["종목코드"])
check(quality_rank[list(picked)].mean() > quality_rank.mean(),
      f"선정 종목의 실제 alpha가 평균 이상 "
      f"({quality_rank[list(picked)].mean():.6f} > {quality_rank.mean():.6f})")

# ── 3. 산출물 형식 ───────────────────────────────────────────
check(perf["수익률"].notna().all() and perf["벤치마크"].notna().all(), "수익률 결측 없음")
check((perf["턴오버"].between(0, 1)).all(), "턴오버가 0~1 범위")
check((perf["비용차감수익률"] <= perf["수익률"] + 1e-12).all(), "비용이 실제로 차감됨")
check(len(res["holdings"]) == s["리밸런싱횟수"] * 30, "보유종목 기록 수 일치")
check(len(s["한계"]) >= 4, f"한계 {len(s['한계'])}가지를 리포트에 명시")

# ── 4. 데이터가 짧으면 조용히 넘어가지 않고 막는지 ───────────
store.write("kor_price", store.read("kor_price").query("날짜 >= '2025-10-01'"))
try:
    backtest.run(market="kr", top_n=30, rebalance="QE")
    check(False, "주가가 짧을 때 막아야 하는데 그냥 돌아감")
except RuntimeError as e:
    check("QUANT_PRICE_KEEP_YEARS" in str(e),
          "주가 기간이 짧으면 명확한 안내와 함께 중단")

shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 엔진이 심어둔 정답을 찾아내고, 미래를 훔쳐보지 않습니다.")
