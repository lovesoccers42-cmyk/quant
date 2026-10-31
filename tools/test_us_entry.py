# -*- coding: utf-8 -*-
"""초기 진입 분할 — 네트워크 없이, 가짜 상태로.

여기서 막으려는 사고
  1. 미국이 1등분이라 첫 달에 100종목을 하루에 담는 것. 신규 매수만
     나눠 들어가야 합니다.
  2. 진입을 '등분을 쪼개서' 나누는 것. 그러면 진입 기간 몇 달 동안 한
     종목이 그 개월에 한 번만 점검돼, 순위에서 밀려난 종목을 몇 달간
     들고 있게 됩니다. 리밸런싱은 **매달 전체**에 돌아야 합니다.
  3. '미매수 표시는 없는데 종목 수가 모자란' 계좌를 진입 완료로 읽는
     것. 남은 종목을 한 달에 몰아서 사라는 주문서가 나옵니다.
  4. 상태가 등분 0~3으로 쪼개진 채 1등분으로 도는 것. 0번 등분의
     종목만 손보고 나머지 75종목은 영구히 방치됩니다.
"""
import os
import sys
import tempfile
from datetime import date

os.environ["QUANT_DATA_DIR"] = tempfile.mkdtemp()
os.environ["QUANT_OUTPUT_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import portfolio as pf  # noqa: E402
import store  # noqa: E402

FAIL = 0
TBL = "us_portfolio"
N = 100


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def _month(i: int) -> date:
    """진입 시뮬레이션용 — 11월부터 i개월 뒤 1일 (해를 넘겨도 됩니다)."""
    return date(2026 + (10 + i) // 12, (10 + i) % 12 + 1, 1)


def put(rows):
    """가짜 보유 상태. rows = [(종목코드, 등분, 기준금액)]"""
    pf.set_market("us")
    store.write(TBL, pd.DataFrame(
        [{"종목코드": c, "등분": j, "편입일": date(2026, 10, 16),
          "기준금액": a, "종목명": f"{c} Corp", "섹터": "Tech"}
         for c, j, a in rows]))


def clear():
    pf.set_market("us")
    store.write(TBL, pd.DataFrame(
        columns=["종목코드", "등분", "편입일", "기준금액", "종목명", "섹터"]))


def held(table=TBL) -> int:
    st = pf.load_state(table)
    if st.empty:
        return 0
    amt = pd.to_numeric(st["기준금액"], errors="coerce")
    return int(len(st) - (amt == 0.0).sum())


pf.set_market("us")

# 모델 순위표 — 앞이 좋은 종목. 200종목이라 상위 100 밖도 있습니다.
picks = pd.DataFrame({
    "종목코드": [f"T{i:03d}" for i in range(200)],
    "종목명": [f"T{i:03d} Corp" for i in range(200)],
    "SEC_NM_KOR": "Tech",
    "종가": np.full(200, 20.0),
    "qvm": np.linspace(0.1, 0.9, 200),
})
HOLD_UNIV = [f"T{i:03d}" for i in range(150)]      # 순위 버퍼 150위
CAP = 100_000.0                                     # 종목당 $1,000

# ── 1. 첫 진입 ───────────────────────────────────────────────
print("[빈 계좌 — 신규 매수만 나눠 담는다]")
clear()
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
check(p["단계"] == "첫진입", f"단계 {p['단계']}")
check(p["등분수"] == 1,
      f"등분은 운용값 1 그대로 — 매달 전체 점검 ({p['등분수']})")
check(p["최대매수"] == 25, f"이번 달 신규 매수 상한 {p['최대매수']}종목")

r = pf.rebalance(picks, n=N, tranches=p["등분수"], capital=CAP,
                 hold_universe=HOLD_UNIV, table=TBL,
                 max_buys=p["최대매수"], today=_month(0))
check(r["매수"] == 25, f"25종목만 샀음 ({r['매수']})")
check(held() == 25, f"보유 25종목 ({held()})")
check(r["미매수"] == 75, f"나머지 75종목은 자리만 ({r['미매수']})")

print("\n[진입 분할을 끄면(1) 한 번에 담는다]")
clear()
p1 = pf.entry_stage(TBL, entry_tranches=1, tranches=1, n=N)
check(p1["최대매수"] == 0, f"상한 없음 ({p1['최대매수']})")
r1 = pf.rebalance(picks, n=N, tranches=1, capital=CAP,
                  hold_universe=HOLD_UNIV, table=TBL,
                  max_buys=p1["최대매수"], today=_month(0))
check(r1["매수"] == N, f"100종목을 한 번에 ({r1['매수']})")

# ── 2. 네 달에 걸쳐 100종목 ──────────────────────────────────
print("\n[네 달에 걸쳐 100종목으로]")
clear()
trail = []
for m in range(4):
    p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
    r = pf.rebalance(picks, n=N, tranches=p["등분수"], capital=CAP,
                     hold_universe=HOLD_UNIV, table=TBL,
                     max_buys=p["최대매수"], today=_month(m))
    trail.append((p["단계"], r["매수"], held()))
    print(f"    {m + 1}개월차  {p['단계']} · 매수 {r['매수']}종목 "
          f"· 보유 {held()}종목")
check([h for _, _, h in trail] == [25, 50, 75, 100],
      f"25 → 50 → 75 → 100 ({[h for _, _, h in trail]})")
check(all(b <= 25 for _, b, _ in trail),
      f"한 달 매수가 상한을 넘지 않음 (최대 {max(b for _, b, _ in trail)})")
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
check(p["단계"] == "운용", f"다 채우면 운용 ({p['단계']})")
check(p["최대매수"] == 0, "운용 단계는 매수 상한 없음")

# ── 3. 진입 중에도 리밸런싱이 매달 도는가 (핵심) ─────────────
print("\n[진입 중에도 매달 전체가 점검된다]")
# 보유 30종목 중 5종목을 순위 버퍼(150위) 밖으로 둡니다 — 팔려야 합니다.
밀려난 = [f"T{150 + i:03d}" for i in range(5)]
보유 = [f"T{i:03d}" for i in range(25)] + 밀려난
put([(c, 0, 1000.0) for c in 보유])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
check(p["단계"] == "진입중", f"보유 30/100 → {p['단계']}")
r = pf.rebalance(picks, n=N, tranches=p["등분수"], capital=CAP,
                 hold_universe=HOLD_UNIV, table=TBL,
                 max_buys=p["최대매수"], today=_month(0))
check(r["매도"] == 5,
      f"순위 밖 5종목이 **진입 중에도** 팔렸음 ({r['매도']})")
남은 = set(pf.load_state(TBL)["종목코드"].astype(str))
check(not (남은 & set(밀려난)), "팔린 종목이 상태에서 빠졌음")
check(r["매수"] == 25, f"매수는 상한 25종목 그대로 ({r['매수']})")

print("\n[비중이 벗어난 종목도 진입 중에 조절된다]")
# 목표 $1,000인데 $3,000짜리 하나 — ±20% 밴드 밖이라 덜어내야 합니다.
put([(f"T{i:03d}", 0, (3000.0 if i == 0 else 1000.0)) for i in range(30)])
r = pf.rebalance(picks, n=N, tranches=1, capital=CAP,
                 hold_universe=HOLD_UNIV, table=TBL,
                 max_buys=25, today=_month(0))
check(r.get("비중조절", 0) >= 1,
      f"비중조절 주문이 나왔음 ({r.get('비중조절')}건)")
오더 = r["orders"]
check(bool((오더["구분"] == "비중축소").any()),
      "넘치는 종목을 덜어내는 주문")

# ── 4. 미국 두 계좌의 실제 상태 ──────────────────────────────
print("\n[미국 main — 58종목에서 두 달 만에 100종목]")
put([(f"T{i:03d}", 0, 1000.0) for i in range(58)])
seq = []
for m in range(2):
    p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
    r = pf.rebalance(picks, n=N, tranches=p["등분수"], capital=CAP,
                     hold_universe=HOLD_UNIV, table=TBL,
                     max_buys=p["최대매수"], today=_month(m))
    seq.append((r["매수"], held()))
    print(f"    {m + 1}개월차  매수 {r['매수']}종목 · 보유 {held()}종목")
check([h for _, h in seq] == [83, 100], f"58 → 83 → 100 ({seq})")

print("\n[미국 alt — 22종목에서 네 달]")
put([(f"T{i:03d}", 0, 1000.0) for i in range(22)])
seq = []
for m in range(4):
    p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
    r = pf.rebalance(picks, n=N, tranches=p["등분수"], capital=CAP,
                     hold_universe=HOLD_UNIV, table=TBL,
                     max_buys=p["최대매수"], today=_month(m))
    seq.append(held())
    print(f"    {m + 1}개월차  매수 {r['매수']}종목 · 보유 {held()}종목")
check(seq == [47, 72, 97, 100], f"22 → 47 → 72 → 97 → 100 ({seq})")
check(max(seq) <= N, "목표를 넘지 않음")

# ── 5. 쪼개진 등분 합치기 ────────────────────────────────────
print("\n[상태가 0~3으로 쪼개져 있으면 합친다]")
put([(f"T{i:03d}", i % 4, 1000.0) for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=N)
check(p["등분정리필요"] is True, "1등분으로 도는데 상태는 4등분 — 정리 필요")
n_merged = pf.merge_slots(TBL, tranches=1)
check(n_merged == 75, f"합친 종목 {n_merged}개 (0번이던 25개는 그대로)")
st = pf.load_state(TBL)
check(set(pd.to_numeric(st["등분"]).unique()) == {0}, "전부 0번 등분")
check(len(st) == 100, f"종목이 사라지지 않음 ({len(st)})")
check(pf.merge_slots(TBL, tranches=1) == 0, "두 번 불러도 아무 일 없음")
check(pf.entry_stage(TBL, entry_tranches=4, tranches=1,
                     n=N)["등분정리필요"] is False, "정리 뒤에는 그만")

print("\n[한국처럼 4등분으로 운용하면 합치지 않는다]")
put([(f"T{i:03d}", i % 4, 1000.0) for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=4, n=N)
check(p["등분수"] == 4 and p["등분정리필요"] is False, "그대로 둠")
try:
    pf.merge_slots(TBL, tranches=2)
    check(False, "2로 합치는 걸 막아야 합니다")
except ValueError as e:
    check("지원하지 않습니다" in str(e), f"막고 이유를 알려줌: {str(e)[:40]}…")

# ── 6. 한국 경로 호환 ───────────────────────────────────────
print("\n[n을 안 주면 판정이 예전과 같다 (한국 경로)]")
put([(f"T{i:03d}", i % 4, 1000.0) for i in range(58)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=4)
check(p["단계"] == "운용" and p["최대매수"] == 0,
      f"목표를 모르면 '운용' · 상한 없음 ({p['단계']})")
print("\n[미매수가 남아 있으면 n 없이도 진입 중]")
put([(f"T{i:03d}", i % 4, (0.0 if i >= 25 else 1000.0)) for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=4)
check(p["단계"] == "진입중" and p["미매수"] == 75,
      f"미매수 {p['미매수']}종목 → {p['단계']}")
check(p["보유종목수"] == 25, f"자리만 잡은 종목은 보유가 아님 ({p['보유종목수']})")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 실제 주문을 내지 마세요.")
    sys.exit(1)
print("전체 통과 — 매수만 나눠 들어가고, 리밸런싱은 매달 전체에 돕니다.")
