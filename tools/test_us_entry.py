# -*- coding: utf-8 -*-
"""초기 진입 분할 — 네트워크 없이, 가짜 상태로.

여기서 막으려는 사고
  1. 미국이 1등분이라 첫 달에 100종목을 하루에 담는 것. 진입만 쪼개고
     리밸런싱 주기는 1등분을 유지해야 합니다.
  2. 진입 등분을 ISO 주차로 고르는 것. 월 1회 실행에서는 달마다 주차가
     4~5주씩 뛰어 같은 등분이 두 번 걸리거나 아예 안 옵니다 — 그러면
     75종목이 영원히 안 들어갑니다.
  3. 진입이 끝났는데 등분을 합치지 않는 것. 상태가 0~3으로 쪼개진 채
     1등분으로 돌면 0번 등분의 25종목만 손보고 나머지는 방치됩니다.
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

import config  # noqa: E402
import portfolio as pf  # noqa: E402
import store  # noqa: E402

FAIL = 0
TBL = "us_portfolio"


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def put(rows):
    """가짜 보유 상태를 깔아 둡니다. rows = [(종목코드, 등분, 기준금액)]"""
    pf.set_market("us")
    store.write(TBL, pd.DataFrame(
        [{"종목코드": c, "등분": j, "편입일": date(2026, 11, 1),
          "기준금액": a, "종목명": f"{c} Corp", "섹터": "Tech"}
         for c, j, a in rows]))


pf.set_market("us")

# ── 1. 첫 진입 ───────────────────────────────────────────────
print("[상태가 비면 진입 분할로 시작한다]")
store.write(TBL, pd.DataFrame(
    columns=["종목코드", "등분", "편입일", "기준금액", "종목명", "섹터"]))
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1)
check(p["단계"] == "첫진입", f"단계 {p['단계']}")
check(p["등분수"] == 4, f"리밸런싱이 1등분이어도 진입은 4등분 ({p['등분수']})")
check(p["slot"] == 0, f"0번 등분부터 ({p['slot']})")

print("\n[진입 분할을 끄면(1) 한 번에 담는다]")
p1 = pf.entry_stage(TBL, entry_tranches=1, tranches=1)
check(p1["등분수"] == 1 and p1["slot"] is None,
      f"등분 {p1['등분수']} · slot {p1['slot']}")

# ── 2. 진입 중 — 등분이 순서대로 돌아야 한다 ─────────────────
print("\n[진입 중에는 안 산 등분 중 가장 작은 번호]")
# 0번은 샀고(금액 있음), 1~3번은 배정만 된 상태
put([(f"T{i:03d}", i % 4, (1000.0 if i % 4 == 0 else 0.0))
     for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1)
check(p["단계"] == "진입중", f"단계 {p['단계']}")
check(p["slot"] == 1, f"다음은 1번 등분 ({p['slot']})")
check(p["미매수"] == 75, f"미매수 75종목 ({p['미매수']})")

# 2번만 남은 상태
put([(f"T{i:03d}", i % 4, (0.0 if i % 4 == 2 else 1000.0))
     for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1)
check(p["slot"] == 2, f"남은 등분이 2번뿐이면 2번 ({p['slot']})")

print("\n[주차가 바뀌어도 등분 순서가 흔들리지 않는다]")
# current_slot은 주차 % 4라 달마다 뜁니다. entry_stage는 주차를 안 봅니다.
weeks = [pf.current_slot(4, date(2026, 11, 1)),
         pf.current_slot(4, date(2026, 12, 1)),
         pf.current_slot(4, date(2027, 1, 1))]
check(len(set(weeks)) < 3,
      f"주차로 고르면 실제로 등분이 겹칩니다 {weeks} — 그래서 안 씁니다")
slots = []
for d in (date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)):
    slots.append(pf.entry_stage(TBL, entry_tranches=4, tranches=1)["slot"])
check(set(slots) == {2}, f"같은 상태면 날짜와 무관하게 같은 답 {slots}")

# ── 3. 진입 완료 → 등분 합치기 ───────────────────────────────
print("\n[다 채우면 등분을 합치고 전체 점검으로 돌아간다]")
put([(f"T{i:03d}", i % 4, 1000.0) for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1)
check(p["단계"] == "운용", f"단계 {p['단계']}")
check(p["등분수"] == 1, f"리밸런싱 등분으로 복귀 ({p['등분수']})")
check(p["slot"] is None, f"등분 지정 없음 ({p['slot']})")
check(p["등분정리필요"] is True, "상태가 0~3으로 쪼개져 있어 정리가 필요")

n = pf.merge_slots(TBL, tranches=1)
check(n == 75, f"등분을 합친 종목 {n}개 (0번이던 25개는 그대로)")
st = pf.load_state(TBL)
check(set(pd.to_numeric(st["등분"]).unique()) == {0},
      "합친 뒤 전부 0번 등분")
check(len(st) == 100, f"종목이 사라지지 않음 ({len(st)})")
check(pf.merge_slots(TBL, tranches=1) == 0, "두 번 불러도 아무 일 없음")
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1)
check(p["등분정리필요"] is False, "정리 뒤에는 더 필요 없다고 답함")

print("\n[한국처럼 4등분으로 운용하는 계좌는 합치지 않는다]")
put([(f"T{i:03d}", i % 4, 1000.0) for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=4)
check(p["등분수"] == 4 and p["등분정리필요"] is False,
      "4등분 운용이면 그대로 둠")
try:
    pf.merge_slots(TBL, tranches=2)
    check(False, "2로 합치는 걸 막아야 합니다")
except ValueError as e:
    check("지원하지 않습니다" in str(e), f"막고 이유를 알려줌: {str(e)[:40]}…")

# ── 4. rebalance가 지정한 등분을 쓰는가 ──────────────────────
print("\n[rebalance(slot=...)이 그 등분만 손본다]")
picks = pd.DataFrame({
    "종목코드": [f"T{i:03d}" for i in range(200)],
    "종목명": [f"T{i:03d} Corp" for i in range(200)],
    "SEC_NM_KOR": "Tech",
    "종가": np.linspace(10.0, 60.0, 200),
    "qvm": np.linspace(0.1, 0.9, 200),
})
put([(f"T{i:03d}", i % 4, 1000.0) for i in range(100)])
for want in (0, 1, 2, 3):
    r = pf.rebalance(picks, n=100, tranches=4, capital=100_000.0,
                     table=TBL, slot=want, today=date(2026, 11, 1))
    check(r["이번등분"] == want, f"slot={want} → 손본 등분 {r['이번등분']}")
r = pf.rebalance(picks, n=100, tranches=4, capital=100_000.0,
                 table=TBL, slot=None, today=date(2026, 11, 1))
check(r["이번등분"] == pf.current_slot(4, date(2026, 11, 1)),
      f"slot을 안 주면 예전처럼 주차로 고름 ({r['이번등분']}번)")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 실제 주문을 내지 마세요.")
    sys.exit(1)
print("전체 통과 — 진입만 쪼개지고, 다 채우면 전체 점검으로 돌아갑니다.")
