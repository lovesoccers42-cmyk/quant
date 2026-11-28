# -*- coding: utf-8 -*-
"""수익률 배틀 계산 점검 (인증키·네트워크 없이).

대시보드 맨 위에 '누가 이기고 있나'를 크게 띄우므로, 리드·역전·라운드
승패가 틀리면 화면 전체가 거짓말이 됩니다. 손으로 셀 수 있는 작은 곡선으로
답을 맞춰 봅니다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import perf  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def curve(days, rets):
    r = pd.Series([0.0] + list(rets), index=days)
    return (1 + r).cumprod()


# 2026-10-12(월) ~ 10-23(금), 거래일 10개
D = pd.bdate_range("2026-10-12", "2026-10-23")

print("[맞대결 — 앞선 날·역전·연속]")
# a가 먼저 앞서다가(3일), 7일째에 b가 역전, 끝까지 b
a = curve(D, [0.01, 0.01, 0.01, -0.02, -0.02, -0.01, 0.0, 0.0, 0.0])
b = curve(D, [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.0, 0.0, 0.0])
h = perf.head_to_head(a, b)
lead_a = int(((a - b) > 1e-9).sum())
lead_b = int(((a - b) < -1e-9).sum())
check(h["앞선날"] == {"a": lead_a, "b": lead_b},
      f"앞선 날 a {h['앞선날']['a']} / b {h['앞선날']['b']} (손셈 {lead_a}/{lead_b})")
check(h["역전"] == 1, f"역전 1번 — {h['역전']}")
check(h["연속"]["누구"] == "b" and h["연속"]["일수"] == lead_b,
      f"지금 b가 {h['연속']['일수']}일째 리드")
check(abs(h["격차"].iloc[-1] - (a.iloc[-1] - b.iloc[-1]) * 100) < 1e-9,
      "격차는 %p (누적 차 × 100)")
check(h["앞선날"]["a"] + h["앞선날"]["b"] < len(D), "첫날 동률(둘 다 1.0)은 안 셈")

# 서로 다른 거래일 — 공통 날짜만
h2 = perf.head_to_head(a.iloc[:7], b.iloc[3:])
check(len(h2["격차"]) == 4, f"공통 거래일만 비교 ({len(h2['격차'])}일)")
check(perf.head_to_head(pd.Series(dtype=float), b)["역전"] == 0, "빈 곡선이면 0")

print("\n[주간 라운드 — 금요일 마감]")
idx = pd.bdate_range("2026-10-12", "2026-10-21")      # 2주차는 수요일까지
ra = curve(idx, [0.02, 0, 0, 0, 0.01, 0, 0])            # 1주 +2%, 2주 +1%
rb = curve(idx, [0.00, 0, 0, 0, 0.03, 0, 0])            # 1주 0%,  2주 +3%
bench = curve(idx, [-0.01, 0, 0, 0, 0, 0, 0])
w = perf.weekly_rounds({"a": ra, "b": rb, "지수": bench})
check(len(w) == 2, f"라운드 2개 — {len(w)}")
check(abs(w["a"].iloc[0] - 0.02) < 1e-12 and w["승자"].iloc[0] == "a",
      f"1R: a +2.00% 승 ({w['a'].iloc[0] * 100:+.2f}%)")
check(abs(w["b"].iloc[1] - 0.03) < 1e-9 and w["승자"].iloc[1] == "b",
      f"2R: b +3.00% 승 ({w['b'].iloc[1] * 100:+.2f}%)")
check(bool(w["진행중"].iloc[1]) and not bool(w["진행중"].iloc[0]),
      "금요일 전에 끝난 마지막 주는 '진행중'")
check(abs(w["지수"].iloc[0] + 0.01) < 1e-12, "지수는 참고로 같은 주 수익률만")
check(w["시작"].iloc[1] == pd.Timestamp("2026-10-19")
      and w["끝"].iloc[1] == pd.Timestamp("2026-10-21"), "라운드 기간 = 실제 첫·끝 거래일")
# 라운드 수익률을 곱하면 전체 수익률과 같아야 합니다
tot = (1 + w["a"]).prod() - 1
check(abs(tot - (ra.iloc[-1] - 1)) < 1e-12, "라운드를 이어 곱하면 누적과 같음")
check(perf.weekly_rounds({"a": ra}).empty, "한쪽이 없으면 빈 표")

print("\n[오늘 등락 · 효자/아픈 종목]")
px = pd.DataFrame({"000001": [100, 110], "000002": [50, 45],
                   "000003": [10, np.nan]},
                  index=pd.to_datetime(["2026-10-20", "2026-10-21"]))
mv = perf.last_day_moves(px, ["1", "000002", "000003"])
check(abs(mv["000001"] - 0.10) < 1e-12 and abs(mv["000002"] + 0.10) < 1e-12,
      "마지막 두 종가로 등락률")
check("000003" not in mv.index, "오늘 종가가 없는 종목은 뺌")

pnl = pd.DataFrame({
    "종목코드": list("ABCDE"), "종목명": list("ABCDE"),
    "손익": [500, -200, 100, np.nan, -900],
    "수익률": [0.25, -0.05, 0.02, np.nan, -0.30]})
hl = perf.pnl_highlights(pnl, n=2)
check(list(hl["효자"]["종목코드"]) == ["A", "C"], "효자 = 수익률 높은 순")
check(list(hl["아픈"]["종목코드"]) == ["E", "B"], "아픈 손가락 = 많이 빠진 순")
check(hl["플러스"] == 2 and hl["계산됨"] == 4 and hl["보유"] == 5,
      "원가미상은 순위·비율에서 빠짐")

print("\n[종합 — 국장 + 미국장 원화 합산]")


def tw(days, values, flows=None):
    v = pd.Series(values, index=pd.to_datetime(days), dtype=float)
    f = pd.Series(flows or {}, dtype=float)
    f.index = pd.to_datetime(f.index)
    return perf.twr(v, f)


kd = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
kr = tw(kd, [1_000_000, 1_000_000, 1_000_000, 1_000_000])
c = perf.combine_markets({"kr": kr, "us": None}, None)
check(np.allclose(c["누적"].values, kr["누적"].values),
      "미국이 없으면 국장 수익률 그대로 (환율 없이도)")

# 10/13에 $1,000 매수(입금), 10/14 +10%, 10/15 환율만 1,000 → 1,100
us = tw(kd[1:], [1000, 1100, 1100], {"2026-10-13": 1000})
fx = pd.Series([1000, 1000, 1000, 1100], index=pd.to_datetime(kd))
c = perf.combine_markets({"kr": kr, "us": us}, fx)
check(abs(c["일수익률"].loc["2026-10-13"]) < 1e-12,
      "미국 첫날: 산 금액만큼 입금으로 잡혀 수익 0")
check(abs(c["일수익률"].loc["2026-10-14"] - 100_000 / 2_000_000) < 1e-12,
      "미국 +10% → 원화 총액 기준 +5% (비중 절반)")
check(abs(c["일수익률"].loc["2026-10-15"] - 110_000 / 2_100_000) < 1e-12,
      "환율만 올라도 원화로는 수익 (+110,000원 / 2,100,000원)")
# 2026-11-22 실측 사고: 미국 첫 보유를 체결 없이 보유목록으로만 올림 → 입금 0
us_seed = tw(kd[1:], [1000, 1100, 1100])                # 순입출금이 하나도 없음
c3 = perf.combine_markets({"kr": kr, "us": us_seed}, fx)
check(abs(c3["일수익률"].loc["2026-10-13"]) < 1e-12,
      "입금 기록 없이 시작한 미국 보유도 첫날 수익 0 (주식 전체가 수익이 되지 않음)")
check(abs(c3["누적"].iloc[-1] - c["누적"].iloc[-1]) < 1e-12,
      "체결내역이 있든 없든 합산 결과가 같음")
check(perf.twr_trust(c3)["신뢰"], "고친 합산 곡선은 신뢰 점검 통과")
broken = perf.twr(c3["평가액"], c3["순입출금"].where(c3.index != pd.Timestamp("2026-10-13"), 0.0))
check(not perf.twr_trust(broken)["신뢰"], "예전처럼 입금이 빠지면 신뢰 점검이 잡아냄")
check(perf.combine_markets({"kr": kr, "us": us}, None).empty,
      "미국이 있는데 환율이 없으면 합치지 않음 (단위가 섞임)")
# 종목 수가 붙은 시장별 곡선(퀀트 종목만)은 합산에도 종목 수를 남겨야
# 합산 곡선의 신뢰 점검이 바구니 크기를 봅니다 (2026-11-28 실측: 시장별로는
# 통과한 10월 초 몇 종목 구간이 합산에서만 '설명 안 되는 변화'로 숨겨짐).
kr_n, us_n = kr.copy(), us.copy()
kr_n["종목수"], us_n["종목수"] = 10, 5
cn = perf.combine_markets({"kr": kr_n, "us": us_n}, fx)
check(list(cn["종목수"]) == [10, 15, 15, 15],
      f"합산 종목 수 = 두 시장 합 ({list(cn['종목수'])})")
check("종목수" not in c.columns, "종목 수가 없는 곡선끼리면 합산에도 없음 (예전 그대로)")

# 거래일이 다름 — 미국만 여는 날(10/16)
us2 = tw(kd[1:] + ["2026-10-16"], [1000, 1000, 1000, 1050], {"2026-10-13": 1000})
fx2 = pd.Series(1000.0, index=pd.to_datetime(kd + ["2026-10-16"]))
c2 = perf.combine_markets({"kr": kr, "us": us2}, fx2)
check(c2["평가액_kr"].loc["2026-10-16"] == 1_000_000,
      "국장이 쉬는 날은 국장 평가액을 전날 값으로")
check(abs(c2["일수익률"].loc["2026-10-16"] - 50_000 / 2_000_000) < 1e-12,
      "그날 수익은 열린 시장 몫만")

# 같은 비중 지수 — 절반씩, 국장 지수 +2%, SPY 0%, 환율 고정 → +1%
kb = pd.Series([1.0, 1.0, 1.02, 1.02], index=pd.to_datetime(kd))
sb = pd.Series([1.0, 1.0, 1.0], index=pd.to_datetime(kd[1:]))
cc = perf.combine_markets({"kr": kr, "us": us}, pd.Series(1000.0, index=pd.to_datetime(kd)))
bb = perf.blended_bench(cc, {"kr": kb, "us": sb},
                        pd.Series(1000.0, index=pd.to_datetime(kd)))
check(abs(bb.loc["2026-10-14"] - 1.01) < 1e-12, "비중 절반씩이면 지수 수익도 절반씩 (+1%)")
check(abs(bb.iloc[0] - 1.0) < 1e-12, "같은 비중 지수는 1.0에서 시작")
# 미국을 사기 전날까지는 국장 지수만
kb2 = pd.Series([1.0, 1.03, 1.03, 1.03], index=pd.to_datetime(kd))
bb2 = perf.blended_bench(cc, {"kr": kb2, "us": sb})
check(abs(bb2.loc["2026-10-13"] - 1.03) < 1e-12, "미국 사기 전에는 국장 지수 100%")

print()
if FAIL:
    print(f"실패 {FAIL}건")
    sys.exit(1)
print("전체 통과")
