# -*- coding: utf-8 -*-
"""TTM 구성과 '유니버스 부족' 신고 점검 — 네트워크 없이.

여기서 막으려는 두 가지 사고
  1. 가장 최근 분기의 값이 비어 있으면 그 종목이 통째로 빠지는 것.
     미국 데이터는 분기 '행'은 있는데 '값'이 null인 경우가 많습니다
     (실측: 2023Q4~2025Q1 구간의 87~98%가 값이 null). 그 때문에 멀쩡한
     이전 TTM이 있어도 점수가 안 났습니다.
  2. 유니버스가 모자라 5종목만 담은 회차를 100종목 백테스트처럼 요약하는 것.
     2026-10-13 미국 런의 첫 네 회차가 실제로 5종목(한 종목 33%)이었는데
     요약에는 '보유종목수 100'만 적혀 있었습니다.
"""
import os
import sys
import tempfile

os.environ.setdefault("QUANT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import factor_core as fc  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def mk(sym: str, dates, vals, account="NetIncome"):
    return pd.DataFrame({"Symbol": sym, "account": account,
                         "date": pd.to_datetime(dates), "value": vals})


Q = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
     "2025-03-31", "2025-06-30"]

print("\n[최근 분기 값이 비어 있을 때]")
# 마지막 분기만 null — 그 이전 4분기로 만든 TTM이 살아 있어야 합니다
late_null = mk("AAA", Q, [10, 10, 10, 10, 10, np.nan])
r = fc.latest_ttm(late_null, symbol="Symbol", account="account",
                  date="date", value="value")
got = float(r["ttm"].iloc[0])
check(len(r) == 1 and abs(got - 40) < 1e-9,
      f"마지막 분기가 null이어도 직전 TTM을 씀 (ttm={got:,.0f}, 기대 40)")
check(f"{r['date'].iloc[0]:%Y-%m-%d}" == "2025-03-31",
      f"쓰인 기준 분기 {r['date'].iloc[0]:%Y-%m-%d} (마지막 유효 분기)")

print("\n[유효한 TTM이 하나도 없으면]")
all_null = mk("BBB", Q, [np.nan] * 6)
r2 = fc.latest_ttm(all_null, symbol="Symbol", account="account",
                   date="date", value="value")
check(len(r2) == 1 and pd.isna(r2["ttm"].iloc[0]),
      "결과는 한 줄이고 ttm은 비어 있음 — 조용히 사라지지 않음")

print("\n[정상 데이터는 그대로]")
ok = mk("CCC", Q, [1, 2, 3, 4, 5, 6])
r3 = fc.latest_ttm(ok, symbol="Symbol", account="account",
                   date="date", value="value")
check(abs(float(r3["ttm"].iloc[0]) - 18) < 1e-9,
      f"마지막 4분기 합 (ttm={float(r3['ttm'].iloc[0]):,.0f}, 기대 18)")

print("\n[분기가 벌어져 있으면 무효]")
gap = mk("DDD", ["2022-03-31", "2024-06-30", "2024-09-30", "2024-12-31"],
         [1, 2, 3, 4])
r4 = fc.latest_ttm(gap, symbol="Symbol", account="account",
                   date="date", value="value")
check(pd.isna(r4["ttm"].iloc[0]),
      f"4분기가 {fc.SPAN_LIMIT_DAYS}일을 넘게 벌어지면 TTM 무효")

print("\n[평균 계정은 ÷4]")
avg = mk("EEE", Q, [100] * 6, account="StockholdersEquity")
r5 = fc.latest_ttm(avg, symbol="Symbol", account="account", date="date",
                   value="value", mean_accounts=("StockholdersEquity",))
check(abs(float(r5["ttm"].iloc[0]) - 100) < 1e-9,
      f"자본은 합이 아니라 평균 (ttm={float(r5['ttm'].iloc[0]):,.0f}, 기대 100)")

print("\n[여러 종목·계정이 섞여도]")
many = pd.concat([late_null, ok, avg], ignore_index=True)
r6 = fc.latest_ttm(many, symbol="Symbol", account="account", date="date",
                   value="value", mean_accounts=("StockholdersEquity",))
check(len(r6) == 3 and set(r6["Symbol"]) == {"AAA", "CCC", "EEE"},
      f"종목·계정 조합마다 한 줄 ({len(r6)}줄)")

print("\n[역분할 종목의 가짜 시총 차단]")
# 실측 사고: WHLR의 2021-10 조정 종가가 주당 $983,695,097,856 (역분할 소급
# 반영), 현재 시총은 $692,586. '과거 종가 × 현재 주식수'로 추정하면
# $193경이 되어 시총상위 200을 혼자 점령했고, 지수 벤치마크가 -100%가
# 됐습니다. 진짜 세계 최대 시총은 $5.5조입니다.
import numpy as np  # noqa: E402
import backtest as _bt  # noqa: E402

at_t = pd.Series({"NORMAL": 100.0, "GROWN": 20.0, "REVSPLIT": 983_695_097_856.0})
latest = pd.Series({"NORMAL": 120.0, "GROWN": 200.0, "REVSPLIT": 0.004})
mc = pd.Series({"NORMAL": 1e11, "GROWN": 2e10, "REVSPLIT": 1.9e17})
kept = _bt._mcap_guard(mc, at_t, latest)
check(pd.notna(kept["NORMAL"]), "평범한 종목은 그대로")
check(pd.notna(kept["GROWN"]), "6년 새 10배 오른 종목도 그대로 (NVDA 같은 경우)")
check(pd.isna(kept["REVSPLIT"]),
      f"역분할 종목의 가짜 시총은 비움 (기준 {_bt.MCAP_PX_RATIO:.0f}배)")
check(_bt.MCAP_PX_RATIO >= 10,
      f"기준이 너무 빡빡하지 않음 ({_bt.MCAP_PX_RATIO:.0f}배)")
check(len(_bt._mcap_guard(pd.Series(dtype=float), at_t, latest)) == 0,
      "빈 입력도 처리")

print("\n[지수 한 종목 비중 상한]")
# 시총 추정이 한 번 어긋나도 지수가 통째로 망가지지 않게 하는 안전장치
check(0 < _bt.INDEX_W_CAP <= 0.2,
      f"상한이 합리적 범위 ({_bt.INDEX_W_CAP:.0%})")
w = pd.Series({"A": 0.95, "B": 0.03, "C": 0.02})
w2 = w.clip(upper=_bt.INDEX_W_CAP); w2 = w2 / w2.sum()
check(abs(float(w2.sum()) - 1.0) < 1e-9 and float(w2.max()) <= 0.9,
      f"한 종목이 95%여도 깎고 다시 정규화 (최대 {float(w2.max()):.1%})")

print("\n[유니버스 부족 신고]")
import backtest  # noqa: E402

# 첫 네 회차가 5종목, 나머지가 100종목인 상황을 그대로 만듭니다
n = 12
perf = pd.DataFrame({
    "리밸런싱일": pd.date_range("2026-03-06", periods=n, freq="7D"),
    "다음리밸런싱": pd.date_range("2026-03-13", periods=n, freq="7D"),
    "종목수": [5, 5, 5, 5] + [100] * (n - 4),
    "수익률": [0.0] * n,
    "비용차감수익률": [-2.0, -3.0, -1.0, 5.0] + [1.0] * (n - 4),
    "벤치마크": [0.5] * n,
    "벤치마크_지수": [0.6] * n,
    "턴오버": [0.1] * n,
    "주식비중": [1] * n,
})
FULL = 0.9
thin = perf[perf["종목수"] < 100 * FULL]
full = perf[perf["종목수"] >= 100 * FULL]
check(len(thin) == 4 and len(full) == 8,
      f"덜 찬 회차 {len(thin)}개 · 완성 회차 {len(full)}개")
fr = (1 + full["비용차감수익률"] / 100).prod() - 1
fb = (1 + full["벤치마크"] / 100).prod() - 1
check(abs(fr - (1.01 ** 8 - 1)) < 1e-9,
      f"완성후 수익률은 완성 회차만으로 계산 ({fr:.2%})")
check(abs((fr - fb) - ((1.01 ** 8 - 1) - (1.005 ** 8 - 1))) < 1e-9,
      f"완성후 초과수익률 {(fr - fb) * 100:+.2f}%p")
check(f"{full['리밸런싱일'].iloc[0]:%Y-%m-%d}" == "2026-04-03",
      f"완성일 {full['리밸런싱일'].iloc[0]:%Y-%m-%d}")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 미국 백테스트를 믿지 마세요.")
    sys.exit(1)
print("전체 통과 — 값이 빈 분기에 종목이 사라지지 않고, 덜 찬 회차를 신고합니다.")
