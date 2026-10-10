# -*- coding: utf-8 -*-
"""perf.py 오프라인 점검 — 수익률 계산이 입금을 수익으로 세지 않는지.

    python tools/test_perf.py

여기서 잡으려는 사고
  · 분할 진입 중 입금이 수익으로 잡히는 것 (가장 위험 — 전략이 망가져도
    수익률이 올라가 보입니다)
  · 체결가 없는 주문(미체결)이 현금흐름·평단에 섞이는 것
  · 원가를 모르는 종목의 손익을 0이나 현재가로 채우는 것
  · 벤치마크 정의가 백테스트와 달라지는 것
"""
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest  # noqa: E402
import perf  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


D = pd.date_range("2026-10-05", "2026-10-16", freq="B")


def mk_price(spec: dict) -> pd.DataFrame:
    """{종목코드: [종가...]} → 주가 긴 표."""
    rows = []
    for code, series in spec.items():
        for d, p in zip(D, series):
            rows.append({"날짜": d, "종목코드": code, "종가": float(p)})
    return pd.DataFrame(rows)


print("\n[벤치마크 정의]")
check(perf.INDEX_N == backtest.INDEX_N,
      f"지수 벤치마크 종목 수가 백테스트와 같음 ({perf.INDEX_N})")

# ── 핵심: 입금을 수익으로 세지 않는가 ───────────────────────────────
print("\n[시간가중수익률 — 입금과 수익 분리]")
# 주가는 10일 내내 10,000원 그대로입니다. 수익은 정확히 0이어야 합니다.
# 그런데 중간에 100주를 더 삽니다(= 100만원 입금). 평가액은 100만 → 200만으로
# 두 배가 되지만, 수익률이 100%로 찍히면 그게 바로 사고입니다.
px = perf.price_pivot(mk_price({"000100": [10000] * len(D)}))
log = pd.DataFrame([
    {"기준일": D[0], "계좌": "alt", "종목코드": "000100", "종목명": "가",
     "평가금액": 1_000_000, "수량": 100},
    {"기준일": D[3], "계좌": "alt", "종목코드": "000100", "종목명": "가",
     "평가금액": 2_000_000, "수량": 200},
])
trades = pd.DataFrame([
    {"기준일": D[3], "계좌": "alt", "구분": "매수", "종목코드": "000100",
     "종목명": "가", "체결가": 10000.0, "체결수량": 100.0, "체결일": D[3]},
])
pos = perf.positions_timeline(log, "alt", px)
val = perf.daily_value(pos, px, end=D[-1])
check(abs(val.iloc[0] - 1_000_000) < 1 and abs(val.iloc[-1] - 2_000_000) < 1,
      f"평가액이 100만 → 200만 ({val.iloc[0]:,.0f} → {val.iloc[-1]:,.0f})")

flows = perf.net_flows(trades, "alt")
check(abs(flows.sum() - 1_000_000) < 1, f"순입금 100만 인식 ({flows.sum():,.0f})")

t = perf.twr(val, flows)
check(abs(t["누적"].iloc[-1] - 1.0) < 1e-9,
      f"주가가 그대로면 수익률 0% — 입금을 수익으로 세지 않음 "
      f"(누적 {t['누적'].iloc[-1]:.6f})")
# 입금을 안 걷어내면 어떻게 되는지도 확인합니다 (이 값이 나오면 버그)
naive = perf.twr(val, None)
check(abs(naive["누적"].iloc[-1] - 2.0) < 1e-9,
      f"입금을 안 빼면 100%로 잘못 나옴 ({naive['누적'].iloc[-1]:.2f}배) "
      f"— 그래서 flows가 필수입니다")

print("\n[실제 수익은 제대로 잡히는가]")
# 주가가 10,000 → 11,000 (+10%), 중간 입금 있음 → 수익률은 10%여야 합니다
ramp = np.linspace(10000, 11000, len(D))
px2 = perf.price_pivot(mk_price({"000100": ramp}))
log2 = pd.DataFrame([
    {"기준일": D[0], "계좌": "alt", "종목코드": "000100", "종목명": "가",
     "평가금액": ramp[0] * 100, "수량": 100},
    {"기준일": D[3], "계좌": "alt", "종목코드": "000100", "종목명": "가",
     "평가금액": ramp[3] * 200, "수량": 200},
])
tr2 = pd.DataFrame([
    {"기준일": D[3], "계좌": "alt", "구분": "매수", "종목코드": "000100",
     "종목명": "가", "체결가": float(ramp[3]), "체결수량": 100.0, "체결일": D[3]},
])
v2 = perf.daily_value(perf.positions_timeline(log2, "alt", px2), px2, end=D[-1])
t2 = perf.twr(v2, perf.net_flows(tr2, "alt"))
got = t2["누적"].iloc[-1] - 1
check(abs(got - 0.10) < 0.002,
      f"주가 +10% · 중간 입금 있어도 수익률 +{got:.2%}")

print("\n[미체결은 현금흐름이 아니다]")
tr3 = pd.concat([tr2, pd.DataFrame([
    # 체결가·체결수량이 빈 행 = 안 일어난 거래
    {"기준일": D[3], "계좌": "alt", "구분": "매도", "종목코드": "999999",
     "종목명": "없는종목", "체결가": np.nan, "체결수량": np.nan,
     "체결일": pd.NaT},
])], ignore_index=True)
check(abs(perf.net_flows(tr3, "alt").sum()
          - perf.net_flows(tr2, "alt").sum()) < 1,
      "체결가 없는 주문은 현금흐름에서 제외")

print("\n[수량 칸이 비어 있어도]")
log4 = pd.DataFrame([
    {"기준일": D[0], "계좌": "main", "종목코드": "000100", "종목명": "가",
     "평가금액": 1_000_000, "수량": np.nan},
])
p4 = perf.positions_timeline(log4, "main", px)
check(abs(float(p4.iloc[0, 0]) - 100) < 1e-6,
      f"평가금액 ÷ 종가로 수량 역산 ({float(p4.iloc[0, 0]):.0f}주)")

print("\n[종목별 손익 — 모르는 원가를 채우지 않는다]")
log5 = pd.DataFrame([
    {"기준일": D[-1], "계좌": "main", "종목코드": "000100", "종목명": "산것",
     "평가금액": ramp[-1] * 100, "수량": 100},
    {"기준일": D[-1], "계좌": "main", "종목코드": "000200", "종목명": "원래있던것",
     "평가금액": 500_000, "수량": 50},
])
px5 = perf.price_pivot(mk_price({"000100": ramp, "000200": [10000] * len(D)}))
tr5 = pd.DataFrame([
    {"기준일": D[0], "계좌": "main", "구분": "매수", "종목코드": "000100",
     "종목명": "산것", "체결가": 10000.0, "체결수량": 100.0, "체결일": D[0]},
])
pnl = perf.position_pnl(tr5, log5, px5, "main")
bought = pnl[pnl["종목코드"] == "000100"].iloc[0]
legacy = pnl[pnl["종목코드"] == "000200"].iloc[0]
check(abs(bought["평단"] - 10000) < 1 and bought["원가출처"] == "원장",
      f"원장에 체결이 있으면 평단이 나옴 ({bought['평단']:,.0f}원)")
check(abs(bought["손익"] - (ramp[-1] - 10000) * 100) < 1,
      f"손익 = (현재가 - 평단) × 수량 ({bought['손익']:,.0f}원)")
check(legacy["원가출처"] == "원가미상" and pd.isna(legacy["손익"]),
      "원가를 모르면 손익을 비움 — 0이나 현재가로 채우지 않음")
cb = pd.Series({"000200": 9_000.0})
pnl2 = perf.position_pnl(tr5, log5, px5, "main", cost_basis=cb)
row = pnl2[pnl2["종목코드"] == "000200"].iloc[0]
check(row["원가출처"] == "입력" and abs(row["손익"] - 1000 * 50) < 1,
      f"매입단가를 넣어주면 손익이 나옴 ({row['손익']:,.0f}원)")

print("\n[지수 벤치마크]")
# 시총 큰 종목이 +20%, 작은 종목이 -20%. 시총가중이면 결과는 +20%에 가깝고,
# 동일가중으로 떨어졌다면 0%에 가깝습니다.
big = np.linspace(10000, 12000, len(D))
small = np.linspace(10000, 8000, len(D))
pxb = perf.price_pivot(mk_price({"000100": big, "000200": small}))
tk = pd.DataFrame([
    {"종목코드": "000100", "시가총액": 1_000_000_000_000.0, "종가": 10000.0,
     "기준일": D[0]},
    {"종목코드": "000200", "시가총액": 10_000_000_000.0, "종가": 10000.0,
     "기준일": D[0]},
])
sh = perf.shares_outstanding(tk)
b = perf.index_benchmark(pxb, sh, start=D[0], end=D[-1], top_n=200)
check(0.18 < b["누적"].iloc[-1] - 1 < 0.21,
      f"시총가중이 지켜짐 (+{b['누적'].iloc[-1] - 1:.2%}, 동일가중이면 0% 근처)")
b1 = perf.index_benchmark(pxb, sh, start=D[0], end=D[-1], top_n=1)
check(abs(b1["누적"].iloc[-1] - b["누적"].iloc[-1]) < 0.01,
      "상위 1종목만 담아도 거의 같음 — 가중이 시총을 따름")

print("\n[회전율]")
val6 = pd.Series([10_000_000.0] * len(D), index=D)
tr6 = pd.DataFrame([
    {"기준일": D[2], "계좌": "main", "구분": "매수", "종목코드": "000100",
     "종목명": "가", "체결가": 10000.0, "체결수량": 250.0, "체결일": D[2]},
])
to = perf.turnover(tr6, val6, "main")
check(abs(float(to.iloc[0]) - 0.25) < 1e-9,
      f"체결 250만 ÷ 평가액 1,000만 = {float(to.iloc[0]):.0%}")

print("\n[규칙 준수]")
state = pd.DataFrame([
    {"종목코드": f"{i:06d}", "등분": i % 4, "기준금액": (0.0 if i >= 96 else 300_000.0)}
    for i in range(100)])
sec = {f"{i:06d}": ("IT" if i < 60 else "산업재") for i in range(100)}
rc = perf.rule_check(state, pd.DataFrame(), "main", sector_map=sec,
                     n_target=100, tranches=4)
check(rc["보유종목수"] == 96 and rc["미매수"] == 4,
      f"미매수(기준금액 0)를 보유로 세지 않음 (보유 {rc['보유종목수']})")
check(rc["종목수편차"] == 0, "목표 100종목과 편차 0")
check(abs(rc["최대섹터비중"] - 60 / 96) < 0.02,
      f"최대 섹터 비중 {rc['최대섹터비중']:.1%} — 금액 기준으로 계산")

print("\n[백테스트 대비 괴리]")
rng = np.random.default_rng(42)
bt = pd.Series(rng.normal(0.0005, 0.01, 2000))
mid = perf.backtest_percentile(
    float((1 + bt).cumprod().pct_change(5).dropna().median()), bt, 5)
check(mid and 0.05 <= mid["분위"] <= 0.95 and mid["판정"] == "정상 범위",
      f"중앙값 근처면 정상 범위 (분위 {mid.get('분위', 0):.0%})")
low = perf.backtest_percentile(-0.25, bt, 5)
check(low and low["분위"] < 0.05 and "확인" in low["판정"],
      f"크게 밑돌면 확인 필요 (분위 {low.get('분위', 0):.1%})")
check(perf.backtest_percentile(0.01, bt.iloc[:3], 5) == {},
      "표본이 모자라면 조용히 빈 결과 — 가짜 판정을 내지 않음")

print("\n[주가가 스냅샷보다 뒤처질 때]")
# 목요일 종가까지만 받고 금요일 저녁에 재동기화하면 흔히 생깁니다.
px_stale = px.loc[px.index <= D[5]]
log_late = pd.DataFrame([
    {"기준일": D[7], "계좌": "alt", "종목코드": "000100", "종목명": "가",
     "평가금액": 1_000_000, "수량": 100}])
v_late = perf.daily_value(perf.positions_timeline(log_late, "alt", px_stale),
                          px_stale)
check(len(v_late) >= 1,
      f"스냅샷이 주가보다 뒤여도 곡선이 비지 않음 ({len(v_late)}점)")

print("\n[첫 주 체결의 회전율]")
# 체결일이 평가액 곡선 시작보다 앞서면 ffill만으로는 분모가 NaN이 됩니다
v_first = pd.Series([10_000_000.0], index=[D[5]])
tr_first = pd.DataFrame([
    {"기준일": D[2], "계좌": "main", "구분": "매수", "종목코드": "000100",
     "종목명": "가", "체결가": 10000.0, "체결수량": 100.0, "체결일": D[2]}])
to_first = perf.turnover(tr_first, v_first, "main")
check(len(to_first) == 1 and abs(float(to_first.iloc[0]) - 0.10) < 1e-9,
      f"곡선보다 앞선 체결도 회전율이 나옴 ({float(to_first.iloc[0]):.0%})")

print("\n[데이터가 없을 때]")
e = pd.DataFrame()
check(perf.price_pivot(pd.DataFrame()).empty,
      "주가가 비어도 빈 표 — 화면에서 주가를 못 받아도 앱이 안 죽음")
check(perf.price_pivot(pd.DataFrame({"a": [1]})).empty, "칸 이름이 달라도 빈 표")
check(perf.positions_timeline(e, "main", px).empty, "보유기록 없으면 빈 표")
check(perf.daily_value(pd.DataFrame(), px).empty, "보유 없으면 빈 곡선")
check(perf.twr(pd.Series(dtype=float)).empty, "평가액 없으면 빈 수익률")
check(perf.net_flows(e, "main").empty, "원장 없으면 빈 현금흐름")
check(perf.position_pnl(e, e, px, "main").empty, "둘 다 없으면 빈 손익표")
check(perf.rule_check(pd.DataFrame(columns=["종목코드", "등분", "기준금액"]),
                      e, "main")["보유종목수"] == 0, "빈 상태도 처리")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 수익률 숫자를 믿지 마세요.")
    sys.exit(1)
print("전체 통과 — 입금이 수익으로 섞이지 않고, 모르는 값은 비워 둡니다.")
