# -*- coding: utf-8 -*-
"""재무제표 기준 시점 점검 — '몇 분기 재무제표로 골랐나' 계산.

대시보드·주문서에 "3분기 31% · 2분기 28% · 1분기 40%"처럼 띄웁니다.
틀리면 오래된 숫자로 고른 종목을 놓치게 되니 손으로 셀 수 있는 예로 맞춥니다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factor_core as fc  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def rows(sym, end, accounts=("NI", "EQ"), filed=None, n=8, freq="QE"):
    qs = pd.date_range(end=end, periods=n, freq=freq)
    out = []
    for a in accounts:
        out.append(pd.DataFrame({"sym": sym, "acct": a, "date": qs,
                                 "val": np.arange(1, n + 1, dtype=float),
                                 "filed": (pd.Series(pd.NaT, index=range(n))
                                           if filed is None else
                                           pd.Series(qs + pd.Timedelta(days=40)))}))
    return out


print("[분기 분포 — 손셈]")
parts = []
parts += rows("A", "2026-09-30", filed=True)        # 3분기 (공시일 있음)
parts += rows("B", "2026-09-30")                    # 3분기
parts += rows("C", "2026-06-30")                    # 2분기
parts += rows("D", "2026-03-31")                    # 1분기
parts += rows("E", "2025-12-31")                    # 4분기 (그 이전)
parts += rows("F", "2026-08-31", freq="3ME")        # 미국식 8월 결산 → 3분기로 묶음
# G: 순이익은 2분기, 자본만 3분기 → 순이익 기준이라 2분기
g = rows("G", "2026-06-30", accounts=("NI",)) + rows("G", "2026-09-30", accounts=("EQ",))
parts += g
fs = pd.concat(parts, ignore_index=True)
ttm = fc.latest_ttm(fs, symbol="sym", account="acct", date="date", value="val",
                    mean_accounts=("EQ",))
q = fc.fs_quarters(ttm, symbol="sym", account="acct", date="date",
                   key_account="NI", filed="filed")
check(q.loc["G", "결산일"] == pd.Timestamp("2026-06-30"),
      "계정마다 분기가 다르면 순이익 분기를 씀")
check(fc.quarter_label(q.loc["F", "결산일"]) == "2026년 3분기",
      "8월 결산은 3분기로 묶음 (달력 분기)")

v = fc.vintage_summary(q, top=3)
dist = {x["분기"]: x["종목수"] for x in v["분포"]}
check(dist == {"2026년 3분기": 3, "2026년 2분기": 2, "2026년 1분기": 1, "그 이전": 1},
      f"분포 {dist}")
check(v["종목수"] == 7 and abs(sum(x["비율"] for x in v["분포"]) - 1) < 1e-3,
      "비율 합 = 1")
check(v["최신분기"] == "2026년 3분기" and v["최신결산일"] == "2026-09-30",
      "최신 분기·결산일")
check(v["주력분기"] == "2026년 3분기", "가장 많은 분기")
check(v["공시일있음"] == 1 and v["최근공시일"] == "2026-11-09",
      "공시일은 있는 종목만 (A: 9/30 + 40일)")

sel = fc.vintage_summary(q, ["C", "D", "없는종목"])
check(sel["종목수"] == 2 and sel["최신분기"] == "2026년 2분기",
      "선정 종목만 세고, 재무가 없는 종목은 뺌")
check(fc.vintage_summary(q.iloc[0:0])["종목수"] == 0, "비면 0")

print("\n[공시 마감이 지난 분기]")
for today, want, due in [("2026-11-25", "2026Q3", "2026-11-14"),
                         ("2026-11-10", "2026Q2", "2026-08-14"),
                         ("2027-03-15", "2026Q3", "2026-11-14"),   # 4분기는 90일
                         ("2027-04-01", "2026Q4", "2027-03-31")]:
    p, d = fc.due_quarter(today)
    check(str(p) == want and f"{d:%Y-%m-%d}" == due,
          f"{today} → {p} (마감 {d:%Y-%m-%d})")
v2 = fc.vintage_summary(q, today="2026-11-25")
check(v2["마감분기"] == "2026년 3분기" and abs(v2["마감분기비율"] - 3 / 7) < 1e-3,
      f"11/25 기준 마감 지난 3분기를 받은 종목 3/7 ({v2['마감분기비율']})")
v3 = fc.vintage_summary(q, today="2026-11-10")
check(v3["마감분기"] == "2026년 2분기" and abs(v3["마감분기비율"] - 5 / 7) < 1e-3,
      "3분기 마감 전(11/10)에는 2분기 이상이면 제때")

print("\n[주문서 한 줄]")
txt = fc.vintage_text({"재무기준": {"선정": sel, "전체": v}})
check(txt.startswith("선정 2종목: 2026년 2분기 50% · 1분기 50%"), txt)
check("전체 7종목: 2026년 3분기 43% · 2분기 29% · 1분기 14% · 그 이전 14%" in txt, txt)
check(fc.pct_text(0.0004) == "<1%" and fc.pct_text(0.31) == "31%",
      "아주 작은 비율은 '<1%' (0%로 보이지 않게)")
check(fc.vintage_text({}) == "기록 없음", "기록이 없으면 '기록 없음'")

print()
if FAIL:
    print(f"실패 {FAIL}건")
    sys.exit(1)
print("전체 통과")
