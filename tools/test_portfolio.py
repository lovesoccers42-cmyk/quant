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


def model(order, prices=None):
    """order: 종목코드 순위 리스트 (앞이 좋은 종목)."""
    df = pd.DataFrame({
        "종목코드": order,
        "종목명": [f"종목{c}" for c in order],
        "SEC_NM_KOR": [f"S{int(c) % 7}" for c in order],
        "qvm": [round(-2 + i * 0.01, 4) for i in range(len(order))],
    })
    if prices is not None:
        df["종가"] = [float(prices.get(c, 10_000)) for c in order]
    return df


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

# ── 6b. 붙여넣은 보유 목록 읽기 ─────────────────────────────
# 엑셀에서 긁어 붙이면 (1) 탭 구분이고 (2) GitHub 입력창이 줄바꿈을 지우고
# (3) 엑셀이 종목코드 앞자리 0을 없앱니다. 실제로 그렇게 들어왔습니다.
print("\n[붙여넣은 목록 읽기]")
PASTE = ("종목코드\t평가금액\t종목명 95570\t310250\tAJ네트웍스 "
         "1040\t649000\tCJ 5930\t6267500\t삼성전자 70\t114600\t삼양홀딩스 "
         "243070\t447810\t휴온스").replace(" ", " ")
p1 = portfolio.parse_holdings(PASTE)
check(len(p1) == 5, f"줄바꿈이 없어도 5종목을 읽어냄 ({len(p1)})")
check(list(p1["종목코드"]) == ["095570", "001040", "005930", "000070", "243070"],
      f"앞자리 0을 되살림 ({list(p1['종목코드'])})")
check(float(p1.loc[p1['종목코드'] == '005930', '평가금액'].iloc[0]) == 6_267_500,
      "금액이 종목과 올바르게 짝지어짐")
check(p1.loc[p1["종목코드"] == "000070", "종목명"].iloc[0] == "삼양홀딩스",
      "이름이 다음 종목코드와 섞이지 않음")

# 제대로 된 CSV, 헤더 없는 CSV, 쉼표 한 줄, 금액에 쉼표·'원'이 붙은 경우
for src, why in (
        ("종목코드,평가금액,종목명\n005930,6267500,삼성전자\n000070,114600,삼양홀딩스",
         "줄바꿈 있는 쉼표 CSV"),
        ("005930,6267500,삼성전자\n000070,114600,삼양홀딩스", "헤더 없는 CSV"),
        ("5930,6267500,삼성전자 70,114600,삼양홀딩스", "줄바꿈 없는 쉼표"),
        ('종목코드,평가금액\n005930,"6,267,500원"\n000070,"114,600원"',
         "금액에 쉼표와 원"),
        ("종목명\t평가금액\t종목코드\n삼성전자\t6267500\t005930\n"
         "삼양홀딩스\t114600\t000070", "컬럼 순서가 다름")):
    got = portfolio.parse_holdings(src)
    ok = (len(got) == 2
          and set(got["종목코드"]) == {"005930", "000070"}
          and float(got.loc[got["종목코드"] == "005930", "평가금액"].iloc[0]) == 6_267_500)
    check(ok, f"{why} → 2종목 정상 ({len(got)}종목)")

# 읽을 수 없는 입력은 조용히 빈 결과를 내지 말고 막아야 합니다
try:
    portfolio.parse_holdings("아무 의미 없는 문장입니다")
    check(True, "의미 없는 입력은 빈 결과 (seed에서 막힘)")
except ValueError:
    check(True, "의미 없는 입력은 명확히 거부")

# ── 7. 보유 종목 seed (이미 주식을 들고 있는 경우) ───────────
print("\n[보유 종목 배분]")
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))

# 금액이 들쭉날쭉한 8종목 3,000만원
amounts = [9_000_000, 7_000_000, 5_000_000, 4_000_000,
           2_000_000, 1_500_000, 1_000_000, 500_000]
held = [{"종목코드": f"{i:06d}", "평가금액": a, "종목명": f"보유{i}"}
        for i, a in enumerate(amounts, start=901)]
sd = portfolio.seed(held, tranches=TR, today=date(2026, 10, 9))
check(sd["보유종목수"] == 8, f"8종목 전부 배분 ({sd['보유종목수']})")
check(abs(sd["총평가액"] - sum(amounts)) < 1, "총평가액이 맞음")
check(sum(sd["등분별금액"]) == sum(amounts), "등분 금액 합이 총액과 같음")
print("    등분별 금액비중:", [f"{s:.1%}" for s in sd["등분별비중"]])
# 금액 기준으로 나눴으니 종목 수가 아니라 금액이 고르게 나와야 합니다
check(max(sd["등분별비중"]) - min(sd["등분별비중"]) < 0.10,
      f"금액이 고르게 나뉨 (최대-최소 {max(sd['등분별비중']) - min(sd['등분별비중']):.1%})")
st7 = portfolio.load_state()
check(len(st7) == 8 and st7["종목코드"].duplicated().sum() == 0, "중복 없이 저장")
check(set(st7["등분"]) <= set(range(TR)), "등분 번호가 범위 안")

# 종목이 등분 수보다 적으면 금액이 쏠려 경고가 떠야 합니다
sd2 = portfolio.seed([{"종목코드": "000001", "평가금액": 3_000_000}],
                     tranches=TR, today=date(2026, 10, 9))
check(sd2["금액균형경고"] is not None, "1종목만 있으면 금액 불균형을 경고")

# 잘못된 입력은 조용히 넘어가지 않아야 합니다
for bad, why in (([], "빈 목록"),
                 ([{"종목코드": "000001"}], "평가금액 컬럼 없음"),
                 ([{"종목코드": "000001", "평가금액": 0}], "평가금액 0")):
    try:
        portfolio.seed(bad, tranches=TR)
        check(False, f"{why}을 그냥 받아들임")
    except ValueError:
        check(True, f"{why}은 명확히 거부")

# seed 후 첫 주문은 '첫 실행'이 아니어야 합니다 (100종목 신규매수 방지)
portfolio.seed(held, tranches=TR, today=date(2026, 10, 9))
r7 = portfolio.rebalance(model(universe), n=N, tranches=TR,
                         today=date(2026, 10, 16))
check(not r7["첫실행"], "seed 후에는 첫 실행으로 보지 않음")
check(r7["매수"] <= 25, f"첫 주에 25종목 이내만 매수 ({r7['매수']}종목)")
check(r7["매도"] <= len([c for c in portfolio.load_state()["종목코드"]]),
      "보유한 것보다 많이 팔지 않음")
print(f"    → 전환 1주차: 매도 {r7['매도']} · 매수 {r7['매수']}")

# ── 8. 수량 계산과 1주 단위 제약 ────────────────────────────
print("\n[수량 계산]")
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
CAP = 30_000_000                       # 3,000만원 → 종목당 30만원
# 상위 5종목을 '1주에 50만원'으로 만들어 배정액을 넘게 합니다
pricey = {c: 500_000 for c in universe[:5]}
px = {c: pricey.get(c, 20_000) for c in universe}
r8 = portfolio.rebalance(model(universe, px), n=N, tranches=TR,
                         today=date(2026, 11, 6), capital=CAP)
check(r8["종목당배정액"] == 300_000, f"종목당 배정액 30만원 ({r8['종목당배정액']:,})")
check(r8["가격초과제외"] == 5, f"배정액보다 비싼 5종목 제외 ({r8['가격초과제외']})")
bought = set(r8["orders"].query("구분 == '매수'")["종목코드"])
check(not (bought & set(universe[:5])), "1주도 못 사는 종목은 주문에 없음")
check(r8["보유종목수"] == N, f"그래도 100종목을 채움 ({r8['보유종목수']})")
qty = r8["orders"].query("구분 == '매수'")["수량"]
check(bool((qty > 0).all()), "모든 매수 종목의 수량이 1주 이상")
check(int(qty.iloc[0]) == 300_000 // 20_000, f"수량 = 배정액÷주가 내림 ({int(qty.iloc[0])}주)")
amt = r8["orders"].query("구분 == '매수'")["예상금액"]
check(bool((amt <= 300_000 + 1).all()), "예상금액이 배정액을 넘지 않음")
print(f"    → 매수 {len(qty)}종목 · 1종목당 {int(qty.iloc[0])}주 "
      f"· 합계 {amt.sum():,.0f}원")

# 자금을 안 주면 수량 칸이 비어야 합니다 (조용히 1주로 넣으면 안 됨)
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
r9 = portfolio.rebalance(model(universe, px), n=N, tranches=TR,
                         today=date(2026, 11, 6))
check(r9["종목당배정액"] is None, "자금 미설정이면 배정액 없음")
check(r9["가격초과제외"] == 0, "자금 미설정이면 가격으로 걸러내지 않음")
check(bool(r9["orders"].query("구분 == '매수'")["수량"].isna().all()),
      "자금 미설정이면 수량 칸이 빔")

# ── 9. 비중 조절 — 전환이 막히지 않는지 ──────────────────────
# 실제 상황: 보유 종목 하나가 전체의 21%입니다(삼성전자). 그게 모델 상위
# 100위에 들어 '유지'로만 처리되면, 그 등분을 팔아 나온 돈이 25종목을 사기에
# 모자라 전환이 멈춥니다. 넘치는 만큼 덜어내는지 확인합니다.
print("\n[비중 조절]")
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
BIG, CAP2 = "005930", 30_000_000
unit2 = CAP2 / N                                  # 30만원
held2 = ([{"종목코드": BIG, "평가금액": 6_300_000, "종목명": "큰종목"}]
         + [{"종목코드": f"{i:06d}", "평가금액": 250_000, "종목명": f"보유{i}"}
            for i in range(701, 796)])            # 96종목 합 2,955만
portfolio.seed(held2, tranches=TR, today=date(2026, 10, 2))
big_slot = int(portfolio.load_state()
               .set_index("종목코드").loc[BIG, "등분"])

# BIG을 1순위에 둔 모델. 주가 7만원이면 목표 30만원은 4주.
order2 = [BIG] + [c for c in universe if c != BIG]
px2 = {c: 20_000 for c in order2}
px2[BIG] = 70_000
# BIG의 등분 차례가 오는 주를 찾습니다
wk = next(d for d in range(2, 40)
          if portfolio.current_slot(TR, date(2026, 10, 2) + pd.Timedelta(weeks=d))
          == big_slot)
day2 = date(2026, 10, 2) + pd.Timedelta(weeks=wk)
r10 = portfolio.rebalance(model(order2, px2), n=N, tranches=TR,
                          today=day2, capital=CAP2)
tr = r10["orders"].query("구분 == '비중축소'")
check(BIG in set(tr["종목코드"]), "21% 종목이 유지되더라도 비중축소 주문이 나옴")
if BIG in set(tr["종목코드"]):
    row = tr[tr["종목코드"] == BIG].iloc[0]
    want = int((6_300_000 - unit2) // 70_000)
    check(int(row["수량"]) == want,
          f"축소 수량 = (현재-목표)÷주가 내림 ({int(row['수량'])}주, 기대 {want}주)")
    check(int(row["예상금액"]) > 5_000_000,
          f"500만원 이상이 현금으로 풀림 ({int(row['예상금액']):,}원)")
check(r10["확보금액_하한"] >= r10["필요금액"] * 0.5,
      f"확보금액이 필요금액에 크게 못 미치지 않음 "
      f"(확보 {r10['확보금액_하한']:,} / 필요 {r10['필요금액']:,})")
print(f"    → 확보(하한) {r10['확보금액_하한']:,}원 / 필요 {r10['필요금액']:,}원")

# ±20% 밴드 안이면 건드리지 않아야 합니다 (거래세만 나갑니다)
near = r10["orders"].query("구분.str.startswith('비중')", engine="python")
st10 = portfolio.load_state()
in_band = [c for c in st10[st10["등분"] == big_slot]["종목코드"]
           if c != BIG and abs(250_000 - unit2) <= unit2 * 0.20]
check(not (set(in_band) & set(near["종목코드"])),
      "목표의 ±20% 안에 있는 종목은 손대지 않음")

# 목표 비중으로 1주도 못 담는 종목은 유지할 수 없습니다
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
portfolio.seed([{"종목코드": "000111", "평가금액": 1_000_000},
                {"종목코드": "000222", "평가금액": 1_000_000},
                {"종목코드": "000333", "평가금액": 1_000_000},
                {"종목코드": "000444", "평가금액": 1_000_000}],
               tranches=TR, today=date(2026, 10, 2))
pricey_code = portfolio.load_state()["종목코드"].iloc[0]
order3 = [pricey_code] + [c for c in universe]
px3 = {c: 20_000 for c in order3}
px3[pricey_code] = 400_000                 # 1주 40만 > 목표 30만
found = False
for d in range(1, 6):
    rr = portfolio.rebalance(model(order3, px3), n=N, tranches=TR,
                             today=date(2026, 10, 2) + pd.Timedelta(weeks=d),
                             capital=CAP2)
    sold = set(rr["orders"].query("구분 == '매도'")["종목코드"])
    if pricey_code in sold:
        found = True
        break
check(found, "1주가 목표 배정액보다 비싸면 유지하지 않고 전량 매도")

shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 붙여넣은 목록을 읽고, 비중을 맞추고, 4주에 전환합니다.")
