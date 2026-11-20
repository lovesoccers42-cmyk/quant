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

print()
if FAIL:
    print(f"실패 {FAIL}건")
    sys.exit(1)
print("전체 통과")
