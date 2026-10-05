# -*- coding: utf-8 -*-
"""실전 분할 리밸런싱 테스트 — 네트워크 없이.

확인하는 것:
  · 첫 실행에 100종목을 한 번에 채우는가
  · 매주 한 등분만 손대는가 (회전율이 안 늘어야 함)
  · 아직 상위권인 종목은 팔지 않는가
  · 같은 종목을 두 등분이 동시에 들지 않는가
  · 한 주를 건너뛰어도 등분 순서가 꼬이지 않는가
  · 같은 주에 두 번 돌려도 같은 등분만 보는가 (중복 주문 방지)
"""
import os
import shutil
import sys
import tempfile
from datetime import date

TMP = tempfile.mkdtemp(prefix="pf_test_")
os.environ["QUANT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import portfolio  # noqa: E402
import store  # noqa: E402

fails = []


def check(cond, msg):
    print(("[OK ] " if cond else "[FAIL] ") + msg)
    if not cond:
        fails.append(msg)


def model(order):
    """order: 종목코드 순위 리스트 (앞이 좋은 종목)."""
    return pd.DataFrame({
        "종목코드": order,
        "종목명": [f"종목{c}" for c in order],
        "SEC_NM_KOR": [f"S{int(c) % 7}" for c in order],
        "qvm": [round(-2 + i * 0.01, 4) for i in range(len(order))],
    })


N, TR = 100, 4
universe = [f"{i:06d}" for i in range(1, 401)]

# ── 1. 첫 실행 ───────────────────────────────────────────────
r1 = portfolio.rebalance(model(universe), n=N, tranches=TR, today=date(2026, 10, 9))
check(r1["첫실행"], "첫 실행으로 인식")
check(r1["보유종목수"] == N, f"100종목을 한 번에 채움 ({r1['보유종목수']})")
check(r1["매수"] == N and r1["매도"] == 0, f"전량 매수 ({r1['매수']}매수/{r1['매도']}매도)")
st = portfolio.load_state()
check(sorted(st["등분"].value_counts().tolist()) == [25, 25, 25, 25],
      f"4등분이 25종목씩 ({st['등분'].value_counts().to_dict()})")

# ── 2. 다음 주 — 한 등분만 ───────────────────────────────────
# 상위 100 중 10종목이 밖으로 밀려나는 상황을 만든다
shifted = universe[10:] + universe[:10]
r2 = portfolio.rebalance(model(shifted), n=N, tranches=TR, today=date(2026, 10, 16))
check(not r2["첫실행"], "두 번째부터는 이어서 진행")
check(r2["매도"] <= 25, f"한 등분(25종목) 이내에서만 매도 ({r2['매도']}종목)")
check(r2["매수"] == r2["매도"], f"판 만큼만 삼 ({r2['매수']}매수/{r2['매도']}매도)")
check(r2["보유종목수"] == N, f"보유 종목 수 유지 ({r2['보유종목수']})")
print(f"    → 이번 등분 {r2['이번등분']} · 매도 {r2['매도']} · 매수 {r2['매수']} · 유지 {r2['유지']}")

st2 = portfolio.load_state()
check(st2["종목코드"].duplicated().sum() == 0, "같은 종목을 두 등분이 동시에 들지 않음")
touched = set(st2[st2["편입일"] == pd.Timestamp("2026-10-16")]["등분"])
check(len(touched) <= 1, f"이번 주에 바뀐 등분은 하나뿐 ({touched})")

# 아직 상위 100 안에 있는데 판 종목이 있으면 안 된다
still_top = set(shifted[:N])
sold = set(r2["orders"].query("구분 == '매도'")["종목코드"])
check(not (sold & still_top), f"상위권 종목을 팔지 않음 (잘못 판 것 {len(sold & still_top)}개)")

# ── 3. 같은 주에 또 돌려도 중복 주문이 없어야 한다 ───────────
r2b = portfolio.rebalance(model(shifted), n=N, tranches=TR, today=date(2026, 10, 16))
check(r2b["이번등분"] == r2["이번등분"], "같은 주에는 같은 등분을 봄")
check(r2b["매도"] == 0 and r2b["매수"] == 0,
      f"이미 맞춰둔 등분이라 주문 없음 ({r2b['매도']}매도/{r2b['매수']}매수)")

# ── 4. 한 주 건너뛰어도 순서가 안 꼬인다 ─────────────────────
slots = [portfolio.current_slot(TR, date(2026, 10, d)) for d in (9, 16, 23, 30)]
check(len(set(slots)) == 4, f"주마다 다른 등분을 봄 {slots}")
check(portfolio.current_slot(TR, date(2026, 10, 16))
      == portfolio.current_slot(TR, date(2026, 10, 14)),
      "같은 주 안에서는 같은 등분 (요일이 달라도)")

# ── 5. 4주 돌면 전체가 한 바퀴 ───────────────────────────────
seen = set()
for wk, d in enumerate((23, 30)):
    rr = portfolio.rebalance(model(shifted), n=N, tranches=TR,
                             today=date(2026, 10, d))
    seen.add(rr["이번등분"])
check(len(seen) == 2, f"주마다 다른 등분을 손봄 {sorted(seen)}")
check(portfolio.load_state()["종목코드"].duplicated().sum() == 0, "중복 보유 없음 (4주 후)")
check(len(portfolio.load_state()) == N, f"4주 후에도 100종목 ({len(portfolio.load_state())})")

# ── 6. 회전율 ───────────────────────────────────────────────
# 구조적 상한: 매주 한 등분(25종목)보다 많이 거래할 수 없습니다.
# 그리고 현실적인 순위 변동에서는 월간 수준(연 200% 안팎)이어야 합니다.
import numpy as np  # noqa: E402


def churn(jitter: int, weeks: int = 24, seed: int = 5) -> tuple[float, int]:
    """jitter가 클수록 매주 순위가 심하게 흔들립니다."""
    store.write("kor_portfolio",
                pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
    rng = np.random.default_rng(seed)
    portfolio.rebalance(model(universe), n=N, tranches=TR, today=date(2026, 1, 2))
    traded = []
    for wk in range(1, weeks + 1):
        order = list(universe)
        idx = rng.choice(len(order), size=jitter, replace=False)
        picked = [order[i] for i in idx]
        rng.shuffle(picked)
        for i, c in zip(idx, picked):
            order[i] = c
        rr = portfolio.rebalance(model(order), n=N, tranches=TR,
                                 today=date(2026, 1, 2) + pd.Timedelta(weeks=wk))
        traded.append(rr["매수"])
    return float(np.mean(traded)) / N, int(max(traded))


mild, mild_max = churn(jitter=20)        # 매주 20종목 순위 뒤섞임 — 현실적
wild, wild_max = churn(jitter=300)       # 매주 전 종목 뒤섞임 — 극단
print(f"\n    순위 변동 보통: 주 {mild*N:.1f}종목 교체 · 연환산 {mild*52*100:.0f}%")
print(f"    순위 변동 극단: 주 {wild*N:.1f}종목 교체 · 연환산 {wild*52*100:.0f}%")

check(mild_max <= 25 and wild_max <= 25,
      f"어떤 경우에도 주당 한 등분(25종목)을 넘지 않음 (최대 {max(mild_max, wild_max)}종목)")
check(mild * 52 < 2.5,
      f"현실적인 순위 변동에서 연환산 회전율이 월간 수준 ({mild*52*100:.0f}%)")
check(wild * 52 < 13.1,
      "극단적으로 흔들려도 구조적 상한(연 1300%) 안에 머무름")

shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 매주 한 등분만 손대고 회전율이 안 늘어납니다.")
