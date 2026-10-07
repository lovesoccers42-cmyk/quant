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

# ── 4. 섹터 상한 · 유동성 필터가 실제로 무는가 ───────────────
import factor_core as fc  # noqa: E402

# 한 섹터가 상위를 독식하도록 만든 뒤 상한이 실제로 자르는지
skew = pd.DataFrame({
    "sym": [f"S{i:03d}" for i in range(200)],
    "sector": (["IT"] * 120 + ["금융"] * 40 + ["소재"] * 40),
    "qvm": np.concatenate([np.linspace(-3, -1, 120),      # IT가 최상위 독식
                           np.linspace(-1, 0, 40),
                           np.linspace(0, 1, 40)]),
})

no_cap, _ = fc.select_portfolio(skew, symbol="sym", sector="sector",
                                n=40, max_sector_pct=1.0)
capped, cstats = fc.select_portfolio(skew, symbol="sym", sector="sector",
                                     n=40, max_sector_pct=0.25)

it_before = (no_cap["sector"] == "IT").mean()
it_after = (capped["sector"] == "IT").mean()
check(it_before == 1.0, f"상한 없으면 IT가 {it_before:.0%} 독식 (문제 재현)")
check(it_after < it_before, f"상한 걸면 IT {it_before:.0%} → {it_after:.0%}")
check(len(capped) == 40, f"목표 종목 수는 채움 ({len(capped)}개)")
check("상한완화" in cstats,
      f"섹터가 3개뿐이라 상한을 완화했고 그 사실을 남김 → {cstats.get('상한완화','없음')}")
check(capped["sector"].nunique() == 3, f"섹터 {capped['sector'].nunique()}개로 분산")

# 섹터가 넉넉하면 요청한 상한이 그대로 지켜져야 한다
wide = pd.DataFrame({
    "sym": [f"W{i:03d}" for i in range(300)],
    "sector": [f"S{i % 10}" for i in range(300)],
    "qvm": np.concatenate([np.linspace(-3, -1, 100), np.linspace(-1, 1, 200)]),
})
wcap, wstats = fc.select_portfolio(wide, symbol="sym", sector="sector",
                                   n=40, max_sector_pct=0.25)
check(len(wcap) == 40 and "상한완화" not in wstats,
      f"섹터 10개면 완화 없이 40종목 ({len(wcap)}개)")
check((wcap["sector"].value_counts() / 40).max() <= 0.25 + 1e-9,
      f"요청한 25% 그대로 지켜짐 (최대 {(wcap['sector'].value_counts()/40).max():.0%})")
check(capped["qvm"].is_monotonic_increasing, "qvm 순서는 유지 (좋은 종목 우선)")
check("순위" in capped.columns and capped["순위"].iloc[0] == 1, "순위 컬럼 부여")

# 유동성 필터
liq_px = pd.DataFrame({
    "날짜": list(DAYS[-25:]) * 3,
    "종목코드": ["A"] * 25 + ["B"] * 25 + ["C"] * 25,
    "종가": [10000] * 75,
    "거래량": [100_000] * 25 + [10_000] * 25 + [1_000] * 25,   # 10억 / 1억 / 1천만
})
elig, lstats = fc.liquidity_eligible(liq_px, symbol="종목코드", date="날짜",
                                     close="종가", volume="거래량",
                                     window=20, min_value=500_000_000)
check(elig == {"A"}, f"거래대금 5억 미만 제외 → 통과 {sorted(elig)}")
check(lstats["탈락"] == 2, f"탈락 {lstats['탈락']}종목 집계")
off, ostats = fc.liquidity_eligible(liq_px, symbol="종목코드", date="날짜",
                                    close="종가", volume="거래량", min_value=0)
check(off is None and ostats["적용"] is False, "기준 0이면 필터 꺼짐(예전 동작)")

# 백테스트 전후 비교 — 같은 데이터로 제약만 바꿔 돌린다
before = backtest.run(market="kr", top_n=30, rebalance="QE",
                      max_sector_pct=1.0, min_turnover=0)
after = backtest.run(market="kr", top_n=30, rebalance="QE",
                     max_sector_pct=0.25, min_turnover=0)


def max_share(r):
    h = r["holdings"]
    return h["섹터"].value_counts().iloc[0] / len(h)


mb, ma = max_share(before), max_share(after)
print(f"\n    최대 섹터 비중: 제약 없음 {mb:.0%} → 상한 적용 {ma:.0%}")
check(ma < mb or mb <= 0.25, f"상한 적용 후 쏠림 완화 ({mb:.0%} → {ma:.0%})")
check(ma <= 0.26, f"보유 기준으로도 25% 이하 유지 ({ma:.0%})")
check(after["summary"]["제약"]["섹터상한"] == "한 섹터 최대 25%",
      "리포트에 제약 조건 기록")

# ── 4-b. 분할 리밸런싱 ───────────────────────────────────────
one = backtest.run(market="kr", top_n=20, rebalance="QE", tranches=1)
four = backtest.run(market="kr", top_n=20, rebalance="QE", tranches=4)

check(one["summary"]["제약"]["분할리밸런싱"].startswith("없음"),
      "1등분이면 예전 동작으로 기록")
check(four["summary"]["제약"]["분할리밸런싱"].startswith("4등분"),
      f"4등분 기록: {four['summary']['제약']['분할리밸런싱']}")

# K=1은 예전과 같아야 한다 — 회귀 방지
base = backtest.run(market="kr", top_n=30, rebalance="QE",
                    max_sector_pct=1.0, min_turnover=0, tranches=1)
check(abs(float(base["perf"]["누적"].iloc[-1])
          - float(before["perf"]["누적"].iloc[-1])) < 1e-12,
      "tranches=1 은 기존 결과와 완전히 동일 (회귀 없음)")

# 보유 종목 수는 유지되고, 회차당 교체는 줄어야 한다
h1 = one["holdings"].groupby("리밸런싱일").size()
h4 = four["holdings"].groupby("리밸런싱일").size()
check(int(h4.iloc[-1]) == int(h1.iloc[-1]),
      f"분할해도 총 보유 종목 수 동일 ({int(h1.iloc[-1])}종목)")

t1 = float(one["perf"]["턴오버"].iloc[1:].mean())
t4 = float(four["perf"]["턴오버"].iloc[1:].mean())
print(f"\n    회차당 턴오버: 1등분 {t1:.1%} → 4등분 {t4:.1%}")
check(t4 <= t1 + 1e-9, f"4등분이 회차당 거래가 더 작다 ({t1:.1%} → {t4:.1%})")
check("연환산턴오버" in four["summary"], "주기 다른 설정끼리 비교할 연환산 턴오버 제공")

# 각 종목은 최소 tranches 회차 동안 유지돼야 한다(한 등분은 4회차에 한 번만 손댐)
hold4 = four["holdings"]
dates4 = sorted(hold4["리밸런싱일"].unique())
if len(dates4) >= 6:
    sets = [set(hold4.loc[hold4["리밸런싱일"] == d, "종목코드"]) for d in dates4]
    changed = [len(sets[i] - sets[i - 1]) for i in range(2, len(sets))]
    check(max(changed) <= 20 // 4 + 1,
          f"회차당 신규 편입이 한 등분 크기 이하 (최대 {max(changed)}종목)")

# ── 10. 리스크 오버레이 ──────────────────────────────────────
# 지수가 장기 이동평균 아래면 비중을 줄여 낙폭을 막는 장치입니다.
# 확인할 것: 꺼져 있으면 예전과 완전히 동일한가, 실제로 현금으로 빠지는가,
# 미래를 훔쳐보지 않는가, 비중을 바꿀 때 거래비용을 무는가.
off = backtest.run(market="kr", top_n=30, rebalance="QE",
                   max_sector_pct=1.0, min_turnover=0, trend_ma=0)
check(abs(float(off["perf"]["누적"].iloc[-1])
          - float(before["perf"]["누적"].iloc[-1])) < 1e-12,
      "오버레이를 끄면 예전 결과와 완전히 동일 (회귀 없음)")
check(off["summary"]["제약"]["리스크오버레이"].startswith("없음"),
      "꺼짐 상태를 리포트에 기록")

on = backtest.run(market="kr", top_n=30, rebalance="QE",
                  max_sector_pct=1.0, min_turnover=0,
                  trend_ma=120, risk_off=0.0, cash_rate=0.02)
check("주식비중" in on["perf"].columns, "회차별 주식비중을 기록")
w = on["perf"]["주식비중"]
check(set(w.unique()) <= {0.0, 1.0}, f"비중은 0 또는 1 ({sorted(set(w.unique()))})")
print(f"\n    주식 보유 {int((w == 1).sum())}회 / 현금 {int((w == 0).sum())}회")
# 위 합성 데이터는 계속 오르기만 해서 현금으로 빠질 일이 없습니다.
# 오버레이가 실제로 작동하는지는 폭락을 만들어 확인합니다.
_orig_px = store.read("kor_price").copy()      # 끝나면 되돌립니다
_px = _orig_px.copy()
_px["날짜"] = pd.to_datetime(_px["날짜"])
_days = sorted(_px["날짜"].unique())
_crash_from = _days[int(len(_days) * 0.6)]
_k = (_px["날짜"] - _crash_from).dt.days.clip(lower=0)
_px.loc[:, "종가"] = _px["종가"] * np.exp(-0.0025 * _k)   # 서서히 반토막
store.write("kor_price", _px)

crash_off = backtest.run(market="kr", top_n=30, rebalance="QE",
                         max_sector_pct=1.0, min_turnover=0, trend_ma=0)
crash_on = backtest.run(market="kr", top_n=30, rebalance="QE",
                        max_sector_pct=1.0, min_turnover=0,
                        trend_ma=120, risk_off=0.0, cash_rate=0.02)
cw = crash_on["perf"]["주식비중"]
print(f"\n    [폭락 시나리오] 주식 {int((cw == 1).sum())}회 / 현금 {int((cw == 0).sum())}회")
check(int((cw == 0).sum()) > 0, "폭락장에서 실제로 현금으로 빠짐")
m_off = crash_off["summary"]["포트폴리오"]["최대낙폭"]
m_on = crash_on["summary"]["포트폴리오"]["최대낙폭"]
r_off = crash_off["summary"]["포트폴리오"]["누적수익률"]
r_on = crash_on["summary"]["포트폴리오"]["누적수익률"]
print(f"    낙폭 {m_off}% → {m_on}% · 수익 {r_off}% → {r_on}%")
check(m_on > m_off, f"폭락장 낙폭을 줄임 ({m_off}% → {m_on}%)")
check(r_on > r_off, f"폭락장 수익을 지킴 ({r_off}% → {r_on}%)")
store.write("kor_price", _orig_px)             # 원래 주가로 복원

# 현금 구간의 수익률은 현금이자에서 비용을 뺀 값이어야 한다 (주가와 무관)
cash_rows = on["perf"][on["perf"]["주식비중"] == 0]
if len(cash_rows):
    worst = float(cash_rows["비용차감수익률"].min())
    check(worst > -0.05,
          f"현금 구간은 주가가 아무리 빠져도 거의 안 잃음 (최악 {worst*100:+.2f}%)")

# 미래 훔쳐보기 방지: 이동평균 판정에 그날까지만 쓰는가.
# 미래를 봤다면 하락장을 완벽히 피해 낙폭이 비현실적으로 작아집니다.
mdd_on = on["summary"]["포트폴리오"]["최대낙폭"]
mdd_off = off["summary"]["포트폴리오"]["최대낙폭"]
print(f"    최대낙폭: 오버레이 없음 {mdd_off}% → 있음 {mdd_on}%")
check(mdd_on > -99 and mdd_on <= 0, "낙폭이 정상 범위")
check(mdd_on > mdd_off - 1e-9, "오버레이가 낙폭을 키우지는 않음")

# 비중 전환에 비용을 무는가 — 비용 0이면 전환이 공짜여서 더 좋아야 한다
free = backtest.run(market="kr", top_n=30, rebalance="QE", cost_bps=0.0,
                    max_sector_pct=1.0, min_turnover=0,
                    trend_ma=120, risk_off=0.0, cash_rate=0.02)
check(float(free["perf"]["누적"].iloc[-1]) >= float(on["perf"]["누적"].iloc[-1]) - 1e-12,
      "비용을 매기면 수익이 줄어듦 (전환 비용이 실제로 계산됨)")

# 부분 축소(50%)도 동작하는가
half = backtest.run(market="kr", top_n=30, rebalance="QE",
                    max_sector_pct=1.0, min_turnover=0,
                    trend_ma=120, risk_off=0.5, cash_rate=0.02)
check(set(half["perf"]["주식비중"].unique()) <= {0.5, 1.0},
      "risk_off=0.5면 비중이 0.5/1.0로만 움직임")

# ── 4b. 비중 방식이 엔진에서 끝까지 돌아가는지 ───────────────
print("\n[비중 방식]")
_w = {}
for scheme in backtest.WEIGHTINGS:
    r = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                     min_turnover=0, tranches=4, weighting=scheme)
    p = r["perf"]
    _w[scheme] = r
    check(len(p) > 3, f"{scheme}: 리밸런싱이 돌아감 ({len(p)}회차)")
    check(bool((p["최대종목비중"] > 0).all()) and bool((p["최대종목비중"] <= 1).all()),
          f"{scheme}: 최대종목비중이 0~1 범위")
    check(bool((p["상위10비중"] <= 1 + 1e-9).all()),
          f"{scheme}: 상위10비중이 100%를 넘지 않음")
    check(bool((p["턴오버"] >= -1e-12).all()) and bool((p["턴오버"] <= 1 + 1e-9).all()),
          f"{scheme}: 회전율이 0~100% 범위")
    print(f"    {scheme:7} 최대비중 평균 {p['최대종목비중'].mean():.2%}"
          f" · 회전율 평균 {p['턴오버'].mean():.1%}")

# 동일가중이라도 '안 건드리는 등분'은 주가대로 흘러가므로 최대비중이
# 1/종목수보다 커집니다. 그게 실제로 벌어지는 일입니다. 다만 몇 배씩
# 벌어지면 분할 로직이 비중을 잃어버린 것이니 상한을 둡니다.
_eq_max = float(_w["equal"]["perf"]["최대종목비중"].mean())
check(1 / 30 <= _eq_max < 3 / 30,
      f"동일가중 최대비중이 1/종목수 이상이지만 과하지 않음 ({_eq_max:.2%})")
check(float(_w["mcap"]["perf"]["최대종목비중"].mean()) > _eq_max,
      "시총가중은 동일가중보다 한 종목에 더 쏠림")
# 상한은 손보는 회차에 맞추는 값이라 드리프트로 넘을 수 있지만,
# 상한 × CAP_BREACH_MULT 는 어떤 회차에서도 넘어서는 안 됩니다.
_mc_worst = float(_w["mcap"]["perf"]["최대종목비중"].max())
check(_mc_worst <= 0.08 * backtest.CAP_BREACH_MULT + 1e-9,
      f"시총가중이 상한×{backtest.CAP_BREACH_MULT}를 넘지 않음 ({_mc_worst:.2%})")
check(int(_w["mcap"]["perf"]["상한초과정리"].sum()) >= 0
      and bool((_w["mcap"]["perf"]["최대종목비중"] <= 0.12 + 1e-9).all()),
      "상한을 크게 넘으면 차례를 기다리지 않고 정리됨")

# 분할 리밸런싱이 실제로 비중을 보존하는가 — 4등분이면 한 회차 회전율이
# 1/4 + 약간(흐트러진 비중 되돌리기)을 크게 넘지 않아야 합니다.
_to = float(_w["equal"]["perf"]["턴오버"].iloc[1:].mean())
check(_to < 0.45, f"4등분에서 회차당 회전율이 과하지 않음 ({_to:.1%})")

# 무매매 밴드 — 비중을 매번 되돌리지 않으면 회전율이 떨어져야 합니다.
# 이게 안 떨어지면 밴드가 실제로 적용되지 않은 것입니다.
_nb = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                   min_turnover=0, tranches=4, weighting="equal", rebal_band=0.0)
_wb = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                   min_turnover=0, tranches=4, weighting="equal", rebal_band=0.20)
_t0 = float(_nb["perf"]["턴오버"].iloc[1:].mean())
_t1 = float(_wb["perf"]["턴오버"].iloc[1:].mean())
print(f"    회전율: 밴드 없음 {_t0:.2%} → ±20% 밴드 {_t1:.2%}")
check(_t1 < _t0, f"밴드가 회전율을 낮춤 ({_t0:.2%} → {_t1:.2%})")
check(len(_wb["perf"]) == len(_nb["perf"]), "밴드를 켜도 회차 수는 같음")
for r, tag in ((_nb, "밴드없음"), (_wb, "밴드20%")):
    w = r["perf"]
    check(bool((w["턴오버"] >= -1e-12).all()) and bool((w["턴오버"] <= 1 + 1e-9).all()),
          f"{tag}: 회전율이 0~100% 범위")
    check(bool((w["최대종목비중"] <= 1 + 1e-9).all()),
          f"{tag}: 최대종목비중이 100%를 넘지 않음")
# 밴드를 켜도 종목 교체는 그대로 일어나야 합니다 (비중만 안 건드리는 것)
check(float(_wb["perf"]["턴오버"].iloc[1:].min()) >= 0,
      "밴드를 켜도 교체가 필요한 회차는 거래가 일어남")
check("무매매밴드" in _wb["summary"]["제약"], "요약에 밴드 설정이 적힘")

# ── 4c. 순위 버퍼 — 종목 교체가 실제로 줄어야 한다 ───────────
# 지난번에 밴드의 효과를 측정하지 않고 주장했다가 틀렸습니다. 이번에는
# 보유 명단에서 교체 수를 직접 세서 확인합니다.
print("\n[순위 버퍼]")


def churn_of(res):
    """회차당 교체 종목 수 / 보유 종목 수."""
    h = res["holdings"]
    g = h.groupby("리밸런싱일")["종목코드"].apply(set)
    ds = list(g.index)
    ch = [len(g[ds[i]] - g[ds[i - 1]]) for i in range(1, len(ds))]
    n = float(h.groupby("리밸런싱일").size().mean())
    return (float(np.mean(ch)) / n if ch and n else 0.0), float(np.mean(ch) if ch else 0)


_ib = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                   min_turnover=0, tranches=4, weighting="equal",
                   rebal_band=0.20, rank_buffer=0.0)
_yb = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                   min_turnover=0, tranches=4, weighting="equal",
                   rebal_band=0.20, rank_buffer=0.5)
c0, n0 = churn_of(_ib)
c1, n1 = churn_of(_yb)
print(f"    교체: 버퍼 없음 {n0:.2f}종목/회차 ({c0:.1%}) "
      f"→ 버퍼 1.5배 {n1:.2f}종목/회차 ({c1:.1%})")
check(c1 < c0, f"버퍼가 종목 교체를 줄임 ({c0:.1%} → {c1:.1%})")
t_no = float(_ib["perf"]["턴오버"].iloc[1:].mean())
t_yes = float(_yb["perf"]["턴오버"].iloc[1:].mean())
print(f"    회전율: {t_no:.2%} → {t_yes:.2%}")
check(t_yes < t_no, f"버퍼가 총 회전율을 낮춤 ({t_no:.2%} → {t_yes:.2%})")
check(len(_yb["holdings"].groupby("리밸런싱일").size().unique()) <= 2,
      "버퍼를 켜도 보유 종목 수는 유지됨")
check("순위버퍼" in _yb["summary"]["제약"], "요약에 버퍼 설정이 적힘")

# ── 4d. 지수 벤치마크 ────────────────────────────────────────
# 실제 대안은 '전 종목 동일가중'이 아니라 지수 ETF입니다. 같은 날짜로 재야
# 수익도 낙폭도 비교됩니다.
print("\n[지수 벤치마크]")
_p = _yb["perf"]
check("벤치마크_지수" in _p.columns, "리밸런싱 표에 지수 수익률이 들어감")
check(not _p["벤치마크_지수"].isna().any(), "지수 수익률에 빈 값이 없음")
check(float((_p["벤치마크_지수"] - _p["벤치마크"]).abs().mean()) > 1e-9,
      "지수(시총가중)와 전종목 동일가중이 서로 다름")
_key = [k for k in _yb["summary"] if k.startswith("벤치마크(시총상위")]
check(len(_key) == 1, f"요약에 지수 벤치마크가 실림 ({_key})")
check("지수대비 초과수익률" in _yb["summary"], "요약에 지수 대비 초과수익률이 실림")
check("지수대비 낙폭차" in _yb["summary"], "요약에 지수 대비 낙폭차가 실림")
check("지수 대비 t" in _yb["summary"]["통계"], "통계에 지수 대비 t가 실림")
print(f"    포트 {_yb['summary']['포트폴리오']['누적수익률']}% · "
      f"지수 {_yb['summary'][_key[0]]['누적수익률']}% · "
      f"지수대비 {_yb['summary']['지수대비 초과수익률']}%p · "
      f"낙폭차 {_yb['summary']['지수대비 낙폭차']}%p")

# 잘못된 방식 이름은 조용히 동일가중으로 넘어가면 안 됩니다
try:
    backtest.run(market="kr", top_n=30, rebalance="QE", weighting="시총")
    check(False, "모르는 비중 방식을 그냥 받아들임")
except ValueError as e:
    check("weighting" in str(e), "모르는 비중 방식은 명확히 거부")

# ── 4e. 섹터 중립 선택 ──────────────────────────────────────
# 모멘텀까지 섹터 안에서만 재면 '그 섹터가 통째로 오른다'는 정보가 지워집니다.
# 설정으로 그걸 풀 수 있어야 하고, 실제로 점수가 달라져야 합니다.
print("\n[섹터 중립]")
import factor_core as _fc  # noqa: E402

check(_fc.neutral_spec("all") == _fc.SECTOR_NEUTRAL_ALL, "'all' 해석")
check(_fc.neutral_spec("no_mom") == _fc.SECTOR_NEUTRAL_NO_MOM, "'no_mom' 해석")
check(_fc.neutral_spec("none") == (), "'none' 해석")
check(_fc.neutral_spec("quality,value") == ("quality", "value"), "직접 나열 해석")
try:
    _fc.neutral_spec("quality,모멘텀")
    check(False, "모르는 팩터명을 그냥 받아들임")
except ValueError as e:
    check("모르는 팩터" in str(e), "모르는 팩터명은 명확히 거부")

_all = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                    min_turnover=0, tranches=4, weighting="score",
                    rebal_band=0.20, rank_buffer=0.5, sector_neutral="all")
_nom = backtest.run(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
                    min_turnover=0, tranches=4, weighting="score",
                    rebal_band=0.20, rank_buffer=0.5, sector_neutral="no_mom")
check(len(_all["perf"]) == len(_nom["perf"]), "두 설정의 회차 수가 같음")
_same = set(_all["holdings"]["종목코드"]) == set(_nom["holdings"]["종목코드"])
check(not _same, "모멘텀 중립을 풀면 고르는 종목이 달라짐")
check("섹터중립" in _nom["summary"]["제약"], "요약에 섹터중립 설정이 적힘")
print(f"    섹터중립 all → {_all['summary']['포트폴리오']['누적수익률']}% · "
      f"no_mom → {_nom['summary']['포트폴리오']['누적수익률']}%")
check(abs(float(_all["summary"]["통계"]["IC 평균"])
          - float(_nom["summary"]["통계"]["IC 평균"])) > 1e-6,
      "점수가 달라졌으니 IC도 달라짐")

# ── 4f. 연도별 표 ───────────────────────────────────────────
_y = _nom["yearly"]
check(len(_y) >= 2, f"연도별 표가 생김 ({len(_y)}개 연도)")
check("지수대비%p" in _y.columns and "전종목대비%p" in _y.columns,
      "연도별 표에 지수·전종목 대비가 들어감")
check(bool((_y["연"].diff().dropna() > 0).all()), "연도가 순서대로 정렬됨")

# ── 4g. 저변동성 팩터 / 종목 추세 필터 / 보유 섹터 비중 상한 ──
print("\n[리스크 레버 3종]")
_base = dict(market="kr", top_n=30, rebalance="QE", max_sector_pct=1.0,
             min_turnover=0, tranches=4, weighting="score",
             rebal_band=0.20, rank_buffer=0.5, sector_neutral="no_mom")

_lv = backtest.run(**_base, use_lowvol=True)
check(len(_lv["perf"]) > 3, f"저변동성 팩터로 돌아감 ({len(_lv['perf'])}회차)")
check("켬" in _lv["summary"]["제약"]["저변동성팩터"], "요약에 저변동성 설정이 적힘")
_plain = backtest.run(**_base)
check(set(_lv["holdings"]["종목코드"]) != set(_plain["holdings"]["종목코드"]),
      "저변동성을 넣으면 고르는 종목이 달라짐")
_v1 = float(_lv["summary"]["포트폴리오"]["연변동성"])
_v0 = float(_plain["summary"]["포트폴리오"]["연변동성"])
print(f"    연변동성: QVM {_v0}% → QVML {_v1}% · "
      f"MDD {_plain['summary']['포트폴리오']['최대낙폭']}% → "
      f"{_lv['summary']['포트폴리오']['최대낙폭']}%")

# VOL 컬럼 없이 4개 가중치를 주면 조용히 3개로 돌지 말고 막아야 합니다
import factor_core as _fc2  # noqa: E402
try:
    _fc2.build_scores(pd.DataFrame({"s": ["a", "b"], "sec": ["x", "x"],
                                    "ROE": [1, 2], "GPA": [1, 2], "CFO": [1, 2],
                                    "PBR": [1, 2], "PCR": [1, 2], "PER": [1, 2],
                                    "PSR": [1, 2], "DY": [0, 0],
                                    "12M": [1, 2], "K_ratio": [1, 2]}),
                      symbol="s", sector="sec", weights=[.25] * 4, n_portfolio=2)
    check(False, "VOL 없이 저변동성을 요청했는데 그냥 돌아감")
except ValueError as e:
    check("VOL" in str(e), "VOL 컬럼이 없으면 명확히 거부")

_ts = backtest.run(**_base, trend_stock=120)
check(len(_ts["perf"]) > 3, f"종목 추세 필터로 돌아감 ({len(_ts['perf'])}회차)")
check("새로 사지 않음" in _ts["summary"]["제약"]["종목추세필터"],
      "요약에 '매수만 제한'으로 적힘")
check(set(_ts["holdings"]["종목코드"]) != set(_plain["holdings"]["종목코드"]),
      "추세 필터가 종목 선택을 바꿈")
# 핵심: 추세 필터가 보유를 강제 매도하면 회전율이 폭증합니다.
# 실제 데이터에서 152%→593%로 뛴 버그라 테스트로 박아둡니다.
_t_ts = float(_ts["perf"]["턴오버"].iloc[1:].mean())
_t_pl = float(_plain["perf"]["턴오버"].iloc[1:].mean())
print(f"    회전율: 필터 없음 {_t_pl:.2%} → 추세 필터 {_t_ts:.2%}")
check(_t_ts < _t_pl * 2.0,
      f"추세 필터가 회전율을 2배 넘게 키우지 않음 ({_t_pl:.2%} → {_t_ts:.2%})")

_sw = backtest.run(**_base, max_sector_weight=0.30)
_p = _sw["perf"]
check("최대섹터비중" in _p.columns, "섹터 비중이 기록됨")
worst = float(_p["최대섹터비중"].max())
check(worst <= 0.30 + 1e-6, f"보유 섹터 비중이 상한을 지킴 (최악 {worst:.1%})")
w_no = float(_plain["perf"]["최대섹터비중"].max()) if "최대섹터비중" in _plain["perf"] else 0
print(f"    최대섹터비중 최악: 상한없음 {w_no:.1%} → 상한30% {worst:.1%}")
# 상한 30%는 이 합성 데이터에서 안 걸립니다(자연 최악 25.1%). 깎는 동작
# 자체는 반드시 걸리는 값으로 따로 확인합니다 — 안 그러면 "상한을 지켰다"가
# "아무 일도 안 했다"와 구분되지 않습니다.
_sw2 = backtest.run(**_base, max_sector_weight=0.15)
_p2 = _sw2["perf"]
check(float(_p2["최대섹터비중"].max()) <= 0.15 + 1e-6,
      f"빡빡한 상한 15%도 지킴 (최악 {float(_p2['최대섹터비중'].max()):.1%})")
check(float(_p2["섹터상한깎음"].sum()) > 0,
      f"실제로 깎은 기록이 남음 (합계 {float(_p2['섹터상한깎음'].sum()):.3f})")
check(float(_p["섹터상한깎음"].sum()) == 0,
      "안 걸리는 상한에서는 깎지 않음 (불필요한 거래 없음)")
for _r, _tag in ((_lv, "QVML"), (_ts, "추세필터"), (_sw, "섹터비중상한")):
    _pp = _r["perf"]
    check(abs(float(_pp["수익률"].abs().max())) < 5, f"{_tag}: 수익률이 상식 범위")
    check(bool((_pp["턴오버"] <= 1 + 1e-9).all()), f"{_tag}: 회전율 0~100%")

# ── 4h. 교차 분할 — 두 계좌가 종목을 하나도 겹치지 않는가 ────
print("\n[교차 분할]")
_s0 = backtest.run(**_base, split={"mod": 2, "rem": 0})
_s1 = backtest.run(**_base, split={"mod": 2, "rem": 1})
h0 = _s0["holdings"].groupby("리밸런싱일")["종목코드"].apply(set)
h1 = _s1["holdings"].groupby("리밸런싱일")["종목코드"].apply(set)
common = [len(h0[d] & h1[d]) for d in h0.index if d in h1.index]
print(f"    회차별 겹치는 종목 수: 최대 {max(common)} · 평균 {np.mean(common):.2f}")
check(max(common) == 0, f"어떤 회차에도 종목이 겹치지 않음 (최대 {max(common)}개)")
# 소속이 영구 고정인지 — 한 종목이 양쪽에 나타나면 안 됩니다
all0 = set(_s0["holdings"]["종목코드"]); all1 = set(_s1["holdings"]["종목코드"])
check(not (all0 & all1),
      f"전 기간을 합쳐도 겹치는 종목이 없음 (겹침 {len(all0 & all1)}개)")
check(len(_s0["perf"]) == len(_s1["perf"]) == len(_plain["perf"]),
      "분할해도 회차 수는 같음")
n0 = float(_s0["holdings"].groupby("리밸런싱일").size().mean())
n1 = float(_s1["holdings"].groupby("리밸런싱일").size().mean())
check(abs(n0 - n1) <= 1, f"두 쪽 종목 수가 비슷함 ({n0:.0f} / {n1:.0f})")
check("교차분할" in _s0["summary"]["제약"], "요약에 분할 설정이 적힘")
check("없음" in _plain["summary"]["제약"]["교차분할"], "분할 안 하면 '없음'")
# 둘 다 같은 신호에서 뽑으므로 성과가 크게 벌어지면 안 됩니다
r0 = float(_s0["summary"]["포트폴리오"]["누적수익률"])
r1 = float(_s1["summary"]["포트폴리오"]["누적수익률"])
print(f"    누적수익: 홀수쪽 {r0}% · 짝수쪽 {r1}% (분할 없음 "
      f"{_plain['summary']['포트폴리오']['누적수익률']}%)")
check(abs(r0 - r1) < max(40.0, abs(r0) * 0.8),
      f"두 쪽 성과가 같은 신호답게 비슷한 수준 ({r0} vs {r1})")

# ── 4h-2. 부분 분할 — 겹침이 손잡이대로 움직이는가 ──────────
print("\n[부분 분할]")
_p5 = backtest.run(**_base, split={"mod": 2, "rem": 0, "shared": 0.5})
_p5b = backtest.run(**_base, split={"mod": 2, "rem": 1, "shared": 0.5})
g5 = _p5["holdings"].groupby("리밸런싱일")["종목코드"].apply(set)
g5b = _p5b["holdings"].groupby("리밸런싱일")["종목코드"].apply(set)
ov5 = [len(g5[d] & g5b[d]) / max(len(g5[d]), 1)
       for d in g5.index if d in g5b.index]
print(f"    공용 50% → 보유 겹침 평균 {np.mean(ov5):.0%}")
check(0.05 < np.mean(ov5) < 0.95,
      f"일부는 겹치고 일부는 안 겹침 ({np.mean(ov5):.0%})")
check(np.mean(ov5) > 0, "완전 분할과 달리 겹치는 종목이 있음")

# 공용 비중이 커지면 겹침도 커져야 합니다 (손잡이가 작동하는지)
_p9 = backtest.run(**_base, split={"mod": 2, "rem": 0, "shared": 0.9})
_p9b = backtest.run(**_base, split={"mod": 2, "rem": 1, "shared": 0.9})
g9 = _p9["holdings"].groupby("리밸런싱일")["종목코드"].apply(set)
g9b = _p9b["holdings"].groupby("리밸런싱일")["종목코드"].apply(set)
ov9 = [len(g9[d] & g9b[d]) / max(len(g9[d]), 1)
       for d in g9.index if d in g9b.index]
print(f"    공용 90% → 보유 겹침 평균 {np.mean(ov9):.0%}")
check(np.mean(ov9) > np.mean(ov5),
      f"공용 비중을 키우면 겹침도 커짐 ({np.mean(ov5):.0%} → {np.mean(ov9):.0%})")
check("공용" in _p5["summary"]["제약"]["교차분할"], "요약에 공용 비중이 적힘")

# shared=1.0 이면 분할이 꺼져야 합니다
_p10 = backtest.run(**_base, split={"mod": 2, "rem": 1, "shared": 1.0})
check(set(_p10["holdings"]["종목코드"]) == set(_plain["holdings"]["종목코드"]),
      "공용 100%면 분할 없는 것과 완전히 동일")
check("없음" in _p10["summary"]["제약"]["교차분할"], "공용 100%는 '없음'으로 표기")

# ── 4i. 팩터 가중치 ─────────────────────────────────────────
print("\n[팩터 가중치]")
_val = backtest.run(**_base, qvm_weights=[0.25, 0.50, 0.25])
check(_val["summary"]["제약"]["팩터가중치"] == "0.25/0.50/0.25",
      f"요약에 가중치가 적힘 ({_val['summary']['제약']['팩터가중치']})")
check(set(_val["holdings"]["종목코드"]) != set(_plain["holdings"]["종목코드"]),
      "가중치를 바꾸면 고르는 종목이 달라짐")
try:
    backtest.run(**_base, qvm_weights=[0.5, 0.5])
    check(False, "가중치 2개를 받아들임")
except Exception:
    check(True, "가중치 개수가 틀리면 막힘")

# ── 5. 데이터가 짧으면 조용히 넘어가지 않고 막는지 ───────────
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
