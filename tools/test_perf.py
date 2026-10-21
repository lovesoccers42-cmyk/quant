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

print("\n[평단 기준 수익률 — 첫 주에도 숫자가 나와야 한다]")
cb = perf.cost_basis_return(pnl)
# pnl: 000100 100주 평단 10,000 → 현재가 ramp[-1]; 000200은 원가미상
exp_cost = 10000 * 100
exp_pl = (ramp[-1] - 10000) * 100
check(cb and abs(cb["원금"] - exp_cost) < 1 and abs(cb["손익"] - exp_pl) < 1,
      f"원가 아는 종목만으로 계산 (원금 {cb.get('원금', 0):,.0f} · "
      f"손익 {cb.get('손익', 0):,.0f})")
check(abs(cb["수익률"] - exp_pl / exp_cost) < 1e-9,
      f"평단 기준 수익률 {cb['수익률']:+.2%}")
check(perf.cost_basis_return(pd.DataFrame()) == {},
      "보유가 없으면 빈 결과")
_nocost = pnl[pnl["원가출처"] == "원가미상"]
check(perf.cost_basis_return(_nocost) == {},
      "원가를 전부 모르면 빈 결과 — 0%로 속이지 않음")

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


print("\n[못 믿을 TWR을 숫자로 내보내지 않는가 — 실제 사고]")
# 2026-10-21: 분할 진입 중이던 배우자 계좌가 9일에 +93.57%로 찍혔습니다.
# 실제 손익은 -145,625원. 원인은 체결일이 '파일을 넣은 날'로 적혀 현금흐름이
# 측정 구간 밖으로 빠진 것 — 그러면 주식을 산 것이 통째로 수익이 됩니다.
_d = pd.bdate_range("2026-10-12", periods=9)
_v = pd.Series([2_000_000, 2_010_000, 2_600_000, 2_610_000, 3_200_000,
                3_190_000, 3_800_000, 3_810_000, 3_860_000],
               index=_d, dtype=float)
_t_bad = perf.twr(_v, None)                      # 현금흐름 누락
_r_bad = float(_t_bad["누적"].iloc[-1] - 1)
check(_r_bad > 0.5,
      f"현금흐름이 없으면 TWR이 터집니다 ({_r_bad:+.1%}) — 재현")
_tr = perf.twr_trust(_t_bad)
check(not _tr["신뢰"], f"그걸 못 믿는다고 판정 ({_tr['점프일수']}일 점프)")
check("체결" in _tr["사유"] or "입출금" in _tr["사유"],
      "무엇을 고쳐야 하는지 사유에 적힘")

_f = pd.Series(0.0, index=_d)
_f.iloc[2] = _f.iloc[4] = _f.iloc[6] = 600_000
_t_ok = perf.twr(_v, _f)                         # 제 날짜에 들어온 경우
_r_ok = float(_t_ok["누적"].iloc[-1] - 1)
check(abs(_r_ok) < 0.05,
      f"현금흐름이 제 날짜에 있으면 실제 수익률 ({_r_ok:+.2%})")
check(perf.twr_trust(_t_ok)["신뢰"], "그건 믿어도 된다고 판정")

# 주가만으로 하루 8%는 못 움직인다는 가정이 100종목에서 타당한지 —
# 평범한 등락은 경고가 뜨면 안 됩니다 (안 그러면 경고가 무의미해집니다)
_calm = pd.Series([1_000_000 * (1.004 ** i) for i in range(20)],
                  index=pd.bdate_range("2026-09-01", periods=20), dtype=float)
check(perf.twr_trust(perf.twr(_calm, None))["신뢰"],
      "하루 0.4%씩 꾸준히 오르는 정상 곡선은 경고 없음")


print("\n[평가액 곡선과 현금흐름의 시계 맞추기 — 2026-10-22 실측]")
# 금요일 10/16 체결 → 일요일 10/18 재동기화 → 월요일 10/19에 평가액 점프.
# 체결일은 10/16으로 **맞는데도** 하루가 어긋나 점프가 수익으로 잡혔습니다.
_dy = pd.bdate_range("2026-10-12", "2026-10-22")
_vv = pd.Series(30_000_000.0, index=_dy)
_vv.loc["2026-10-19":] = 36_231_798.0
_fl = pd.Series({pd.Timestamp("2026-10-16"): 6_200_000.0})
_sn = pd.DatetimeIndex(["2026-10-12", "2026-10-18"])

_t0 = perf.twr(_vv, _fl)
check(not perf.twr_trust(_t0)["신뢰"],
      f"옮기기 전에는 못 믿는다고 판정 ({(_t0['누적'].iloc[-1]-1):+.2%})")
_al = perf.align_flows(_fl, _vv.index, _sn)
check(list(_al.index) == [pd.Timestamp("2026-10-19")],
      f"현금흐름이 10/19로 옮겨짐 ({[f'{d:%m/%d}' for d in _al.index]})")
check(abs(float(_al.iloc[0]) - 6_200_000) < 1, "금액은 그대로")
_t1 = perf.twr(_vv, _al)
check(perf.twr_trust(_t1)["신뢰"],
      f"옮긴 뒤에는 믿을 수 있음 ({(_t1['누적'].iloc[-1]-1):+.2%})")
check(abs(float(_t1["누적"].iloc[-1] - 1)) < 0.02,
      f"수익률이 현실적인 범위 ({(_t1['누적'].iloc[-1]-1):+.2%})")

print("\n[아직 스냅샷에 안 잡힌 거래는 현금흐름에서 뺀다]")
# 마지막 스냅샷 뒤에 산 종목은 평가액 곡선이 아직 모릅니다(옛 수량으로
# 계산 중). 현금흐름만 넣으면 없는 손실이 생깁니다.
_late = pd.Series({pd.Timestamp("2026-10-21"): 5_000_000.0})
_al2 = perf.align_flows(_late, _vv.index, _sn)
check(len(_al2) == 0, f"스냅샷 뒤 거래는 버림 ({len(_al2)}건)")

print("\n[옮길 것이 없으면 그대로]")
_fl3 = pd.Series({pd.Timestamp("2026-10-13"): 1_000_000.0})
_al3 = perf.align_flows(_fl3, _vv.index, pd.DatetimeIndex(["2026-10-13"]))
check(list(_al3.index) == [pd.Timestamp("2026-10-13")],
      "스냅샷과 체결일이 같은 거래일이면 제자리")
check(len(perf.align_flows(pd.Series(dtype=float), _vv.index, _sn)) == 0,
      "빈 현금흐름도 처리")
check(len(perf.align_flows(_fl, _vv.index, pd.DatetimeIndex([]))) == 0,
      "스냅샷이 없으면 빈 결과")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 수익률 숫자를 믿지 마세요.")
    sys.exit(1)
print("전체 통과 — 입금이 수익으로 섞이지 않고, 모르는 값은 비워 둡니다.")
