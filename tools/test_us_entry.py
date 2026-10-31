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


def _month(i: int) -> date:
    """진입 시뮬레이션용 — 11월부터 i개월 뒤 1일 (해를 넘겨도 됩니다)."""
    return date(2026 + (10 + i) // 12, (10 + i) % 12 + 1, 1)


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


# ── 5. 진입이 덜 끝난 계좌 (미매수 표시가 없는 경우) ─────────
# 지금 미국 두 계좌가 이 상태입니다 — 보유 58·22종목인데 미매수는 0.
# 체결이 덜 됐거나 보유 목록으로 resync를 돌려 미매수 표시가 지워지면
# 이렇게 됩니다. n을 안 보면 '진입 완료'로 읽고 남은 42종목을 한 달에
# 몰아서 사라는 주문서가 나옵니다.
print("\n[보유가 목표에 모자라면 미매수 표시가 없어도 진입 중]")
put([(f"T{i:03d}", 0, 1000.0) for i in range(58)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
check(p["단계"] == "진입중", f"58/100종목 → 단계 {p['단계']}")
check(p["미매수"] == 0, "미매수 표시는 0인데도 진입 중으로 봄")
check(p["등분재배분필요"] is True,
      "58종목이 전부 0번 등분에 몰려 있어 재배분이 필요")
check(pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=0)["단계"] == "운용",
      "n을 안 주면 예전처럼 '운용'으로 읽음 (한국 경로 호환)")

print("\n[등분 재배분 — 금액이 고르게]")
sp = pf.respread_slots(TBL, tranches=4)
check(sp["보유종목수"] == 58, f"종목이 사라지지 않음 ({sp['보유종목수']})")
cnt = sp["등분별종목수"]
check(max(cnt.values()) - min(cnt.values()) <= 1,
      f"등분별 종목 수가 고름 {cnt}")
st = pf.load_state(TBL)
amt_by = (pd.to_numeric(st["기준금액"], errors="coerce")
          .groupby(pd.to_numeric(st["등분"], errors="coerce")).sum())
check(float(amt_by.max() - amt_by.min()) <= float(amt_by.max()) * 0.2,
      f"등분별 금액도 고름 (최대-최소 {float(amt_by.max()-amt_by.min()):,.0f})")
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
check(p["등분재배분필요"] is False, "재배분 뒤에는 더 필요 없다고 답함")

print("\n[네 달에 걸쳐 100종목으로 수렴하는가]")
held = []
for month in range(4):
    p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
    if p["등분재배분필요"]:
        pf.respread_slots(TBL, tranches=p["등분수"])
        p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
    r = pf.rebalance(picks, n=100, tranches=p["등분수"], capital=100_000.0,
                     table=TBL, slot=p["slot"],
                     today=_month(month))
    n_now = len(pf.load_state(TBL))
    held.append((p["slot"], r["매수"], n_now))
    print(f"    {month + 1}개월차  등분 {p['slot']}번 · 매수 {r['매수']}종목 "
          f"· 보유 {n_now}종목")
check(all(n <= 100 for _, _, n in held),
      f"목표를 넘지 않음 (최대 {max(n for _, _, n in held)}종목)")
check(held[-1][2] == 100, f"4개월 뒤 100종목 ({held[-1][2]})")
check(max(b for _, b, _ in held) <= 25,
      f"한 달에 담는 종목이 한 등분 치를 넘지 않음 "
      f"(최대 {max(b for _, b, _ in held)}종목)")
check(len({s for s, _, _ in held}) == 4,
      f"네 등분을 한 번씩 돌았음 {[s for s, _, _ in held]}")
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
check(p["단계"] == "운용" and p["등분정리필요"] is True,
      "다 채웠으니 운용 단계 + 등분 정리 대기")

print("\n[재배분을 건너뛰면 실제로 목표를 넘는다 — 그래서 필요합니다]")
put([(f"T{i:03d}", 0, 1000.0) for i in range(58)])
for month in range(3):
    p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
    pf.rebalance(picks, n=100, tranches=4, capital=100_000.0,
                 table=TBL, slot=p["slot"], today=_month(month))
over = len(pf.load_state(TBL))
check(over > 100, f"재배분 없이 세 달 돌리면 {over}종목 — 목표 100을 넘김")


# ── 6. 넘치지는 않지만 속도가 들쭉날쭉한 계좌 ────────────────
# alt(미국)이 이 경우입니다 — 22종목이 전부 0번 등분. 자리(25)를 넘지
# 않으니 보유가 목표를 넘지는 않지만, 25·25·25·3종목으로 들어갑니다.
print("\n[보유가 한 등분에만 있으면 넘치지 않아도 고르게 나눈다]")
put([(f"T{i:03d}", 0, 1000.0) for i in range(22)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
check(p["단계"] == "진입중", f"22/100종목 → {p['단계']}")
check(p["등분재배분필요"] is True,
      "자리(25)를 넘지는 않지만 한 등분에만 몰려 있어 재배분")
sp = pf.respread_slots(TBL, tranches=4)
cnt = sp["등분별종목수"]
check(max(cnt.values()) - min(cnt.values()) <= 1, f"고르게 나뉨 {cnt}")
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
check(p["등분재배분필요"] is False, "나눈 뒤에는 그만")

print("\n[22종목이 네 달에 걸쳐 고르게 들어가는가]")
buys = []
for month in range(4):
    p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
    r = pf.rebalance(picks, n=100, tranches=p["등분수"], capital=100_000.0,
                     table=TBL, slot=p["slot"], today=_month(month))
    buys.append(r["매수"])
    print(f"    {month + 1}개월차  등분 {p['slot']}번 · 매수 {r['매수']}종목 "
          f"· 보유 {len(pf.load_state(TBL))}종목")
check(len(pf.load_state(TBL)) == 100, "4개월 뒤 100종목")
check(max(buys) - min(buys) <= 2,
      f"월별 매수가 고름 {buys} (재배분 없으면 25·25·25·3)")

print("\n[이미 네 등분을 쓰고 있으면 건드리지 않는다]")
put([(f"T{i:03d}", i % 4, (1000.0 if i % 4 == 0 else 0.0))
     for i in range(100)])
p = pf.entry_stage(TBL, entry_tranches=4, tranches=1, n=100)
check(p["등분재배분필요"] is False,
      "첫 진입 직후(0번만 샀고 1~3번은 배정만) 상태는 그대로")
