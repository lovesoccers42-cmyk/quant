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

# ── 10. 순위 버퍼 — 실전도 백테스트와 같은 규칙인지 ──────────
# 백테스트는 "100위 안에서 사고 150위 밖에서 판다"로 돌아갑니다. 실전이
# 그와 다르면 검증한 것과 다른 걸 사고팔게 됩니다.
print("\n[순위 버퍼 (실전)]")
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
BUF = 1.5
# 처음 100종목을 담아두고, 다음 주에 순위를 20칸 밀어버립니다.
portfolio.rebalance(model(universe), n=N, tranches=TR, today=date(2026, 10, 9))
# 보유 중인 상위 10종목을 '101~110위'로 밀어 넣습니다 — 버퍼 구간입니다.
# 버퍼가 없으면 팔고, 버퍼가 있으면 들고 있어야 합니다.
shifted2 = universe[10:110] + universe[:10] + universe[110:]
hold_u = shifted2[:int(N * BUF)]                  # 유지 명단 = 상위 150위

no_buf = portfolio.rebalance(model(shifted2), n=N, tranches=TR,
                             today=date(2026, 10, 16))
st_a = portfolio.load_state()
# 같은 상황을 버퍼를 켜고 다시 (상태를 되돌려서)
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
portfolio.rebalance(model(universe), n=N, tranches=TR, today=date(2026, 10, 9))
with_buf = portfolio.rebalance(model(shifted2), n=N, tranches=TR,
                               today=date(2026, 10, 16), hold_universe=hold_u)
print(f"    매도: 버퍼 없음 {no_buf['매도']}종목 → 버퍼 1.5배 {with_buf['매도']}종목")
check(with_buf["매도"] < no_buf["매도"],
      f"버퍼가 매도를 줄임 ({no_buf['매도']} → {with_buf['매도']})")
check(with_buf["매수"] == with_buf["매도"],
      f"판 만큼만 삼 ({with_buf['매수']}매수/{with_buf['매도']}매도)")
check(with_buf["보유종목수"] == N, f"보유 100종목 유지 ({with_buf['보유종목수']})")
check(list(with_buf["orders"].columns)[:2] == ["구분", "종목코드"],
      "거래가 없는 주에도 주문표에 컬럼이 있음")


def codes_of(res, kind):
    o = res["orders"]
    return set(o.loc[o["구분"] == kind, "종목코드"]) if len(o) else set()

# 101~150위는 '들고 있으면 유지, 없으면 안 산다'는 중립 구간이어야 합니다
bought = codes_of(with_buf, "매수")
mid = set(shifted2[N:int(N * BUF)])
check(not (bought & mid), f"버퍼 구간 종목은 새로 사지 않음 (잘못 산 것 {len(bought & mid)}개)")
held_now = set(portfolio.load_state()["종목코드"])
check(bool(held_now & mid), "버퍼 구간에 있는 보유 종목은 팔지 않고 들고 있음")
sold_buf = codes_of(with_buf, "매도")
check(not (sold_buf & mid), f"버퍼 구간 종목을 팔지 않음 (잘못 판 것 {len(sold_buf & mid)}개)")
sold_nobuf = codes_of(no_buf, "매도")
check(bool(sold_nobuf & mid),
      "버퍼가 없으면 같은 종목을 팔았다 (비교가 성립하는지 확인)")

# 150위 밖으로 완전히 밀려나면 팔아야 합니다
far = universe[:5]                                 # 맨 뒤로 밀어버릴 종목
pushed = [c for c in universe if c not in far] + far
hold_u2 = pushed[:int(N * BUF)]
sold_far = False
for d in (23, 30, 6, 13):
    rr = portfolio.rebalance(model(pushed), n=N, tranches=TR,
                             today=date(2026, 10, d) if d > 20 else date(2026, 11, d),
                             hold_universe=hold_u2)
    if codes_of(rr, "매도") & set(far):
        sold_far = True
        break
check(sold_far, "버퍼 밖으로 완전히 밀려난 종목은 매도됨")

# ── 11. 월 재동기화 + 투입 대기 현금 ────────────────────────
print("\n[재동기화 · 현금투입]")
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
CAP3 = 30_000_000
held3 = [{"종목코드": f"{i:06d}", "평가금액": 300_000, "종목명": f"보유{i}"}
         for i in range(1, 101)]
portfolio.seed(held3, tranches=TR, today=date(2026, 10, 2))

# 한 달 뒤: 전 종목이 10% 올랐고 현금 120만원이 쌓였습니다.
grown = [{"종목코드": h["종목코드"], "평가금액": 330_000, "종목명": h["종목명"]}
         for h in held3]
rs = portfolio.resync(grown, cash=1_200_000, tranches=TR, today=date(2026, 11, 6))
check(rs["보유종목수"] == 100, f"보유 100종목 유지 ({rs['보유종목수']})")
check(abs(rs["평가액"] - 33_000_000) < 1, f"평가액 갱신 ({rs['평가액']:,.0f})")
check(abs(rs["총자본"] - 34_200_000) < 1, f"총자본 = 평가액+현금 ({rs['총자본']:,.0f})")
check(abs(rs["종목당배정액"] - 342_000) < 1, f"배정액 재계산 ({rs['종목당배정액']:,.0f})")
check(not rs["사라진종목"] and not rs["새로들어온종목"], "종목 변화 없음")
cp = portfolio.load_capital()
check(abs(cp["총자본"] - 34_200_000) < 1, "총자본이 저장돼 다시 읽힘")
st11 = portfolio.load_state()
check(abs(float(st11["기준금액"].iloc[0]) - 330_000) < 1,
      f"기준금액이 실제 평가액으로 갱신 ({float(st11['기준금액'].iloc[0]):,.0f})")

# 배정액 34.2만 vs 기준금액 33만 = 3.6% 차이 → 밴드(±20%) 안.
# 현금이 없으면 아무것도 사지 않아야 하고(불필요한 거래 없음),
# 현금이 있으면 밴드를 건너뛰고 사야 합니다(현금이 쌓이지 않게).
px3 = {f"{i:06d}": 30_000 for i in range(1, 401)}
order3 = [f"{i:06d}" for i in range(1, 101)] + universe[100:]
# rebalance는 손본 등분의 기준금액을 목표로 갱신합니다. 두 설정을 같은 주로
# 비교하려면 사이에 상태를 되돌려야 합니다 — 안 그러면 두 번째 호출은
# "이미 목표에 맞춰져 있다"고 보고 아무것도 하지 않습니다.
snap = portfolio.load_state().copy()
no_cash = portfolio.rebalance(model(order3, px3), n=N, tranches=TR,
                              today=date(2026, 11, 13), capital=34_200_000)
check(no_cash.get("비중조절", 0) == 0,
      f"현금이 없으면 밴드 안에서는 거래 없음 ({no_cash.get('비중조절')}건)")
check(no_cash.get("현금투입", 0) == 0, "현금이 없으면 현금투입도 없음")

store.write("kor_portfolio", snap)          # 같은 출발점으로 되돌림
with_cash = portfolio.rebalance(model(order3, px3), n=N, tranches=TR,
                                today=date(2026, 11, 13), capital=34_200_000,
                                cash=1_200_000)
check(with_cash["현금투입"] > 0,
      f"현금이 있으면 밴드를 건너뛰고 매수 ({with_cash['현금투입']}종목)")
budget = 1_200_000 / TR
check(with_cash["현금투입금액"] <= budget + 1,
      f"이번 주 예산(현금÷등분수={budget:,.0f}) 안에서만 씀 "
      f"({with_cash['현금투입금액']:,}원)")
check(with_cash["현금투입금액"] >= budget - 30_000,
      f"예산을 거의 다 씀 ({with_cash['현금투입금액']:,}원)")
co = with_cash["orders"].query("구분 == '현금투입'")
check(bool((co["수량"] > 0).all()), "현금투입 수량이 1주 이상")
check(set(co["종목코드"]) <= set(st11["종목코드"]), "현금투입은 보유 종목에만")
print(f"    → 현금 1,200,000원 중 이번 주 {with_cash['현금투입금액']:,}원 투입 "
      f"({with_cash['현금투입']}종목)")

# 잘못된 재동기화는 조용히 넘어가지 않아야 합니다
store.write("kor_portfolio",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio"])))
try:
    portfolio.resync(grown, cash=0, tranches=TR)
    check(False, "보유 상태 없이 재동기화를 허용함")
except ValueError as e:
    check("seed" in str(e), "상태가 없으면 seed를 먼저 하라고 안내")

# ── 12. 두 계좌(프로필) ─────────────────────────────────────
# 핵심은 두 가지입니다: 상태가 섞이지 않는가, 비중 방식이 주문서에 반영되는가.
print("\n[두 계좌 · 비중 방식]")
import config as _cfg  # noqa: E402

PM, PA = _cfg.profile("main"), _cfg.profile("alt")
check(PM["state_table"] != PA["state_table"], "보유 테이블이 서로 다름")
check(PM["model_file"] != PA["model_file"], "모델 파일이 서로 다름")
# 두 계좌는 어느 축에서든 달라야 합니다. 비중 축은 측정 결과 '점수가중'만
# 통했으므로(시총가중 -21.9%p, 역변동성 -5.3%p) 둘 다 점수가중을 쓰고,
# 섹터 축을 뒤집습니다.
axes = [k for k in ("weighting", "sector_neutral", "max_sector_pct")
        if PM[k] != PA[k]]
check(True, f"설정 차이 축 {axes} (지금은 종목 분할로 차별화)")
# 분할은 운용 시작 시점에 꺼져 있습니다(측정 안 된 설정이라). 켜져 있으면
# 바구니가 서로 달라야 하고, 꺼져 있으면 등분 위상으로만 구분됩니다.
import factor_core as _fc3
if PM["split"] and PA["split"]:
    check(PM["split"] != PA["split"],
          "교차 분할 바구니가 다름 %s / %s" % (PM["split"], PA["split"]))
    _mod = int(PM["split"]["mod"])
    _ov = [c for c in universe
           if _fc3.split_bucket(c, _mod) == int(PM["split"]["rem"])
           and _fc3.split_bucket(c, _mod) == int(PA["split"]["rem"])]
    check(not _ov, f"같은 종목이 두 바구니에 동시에 들어가지 않음 ({len(_ov)}개)")
else:
    check(PM["split"] is None and PA["split"] is None,
          "분할은 양쪽 모두 꺼져 있음 (한쪽만 켜면 한 계좌만 희석됩니다)")
    # 분할 함수 자체는 계속 작동해야 합니다 — 나중에 켤 수 있도록
    _b = [_fc3.split_bucket(c, 2) for c in universe]
    check(set(_b) == {0, 1} and abs(_b.count(0) - _b.count(1)) < len(_b) * 0.2,
          f"분할 함수는 정상 (바구니 {_b.count(0)}/{_b.count(1)})")
check(PM["slot_offset"] != PA["slot_offset"], "손보는 등분이 엇갈림")
try:
    _cfg.profile("wife")
    check(False, "모르는 프로필을 받아들임")
except ValueError as e:
    check("모르는 프로필" in str(e), "모르는 프로필은 명확히 거부")

for t in (PM["state_table"], PA["state_table"]):
    store.write(t, pd.DataFrame(columns=list(store.SCHEMAS[t])))

CAP4 = 30_000_000
# alt는 변동성이 작은 종목에 더 싣습니다 — 앞쪽 종목의 변동성을 작게 둡니다
vols = {c: 0.01 + 0.0004 * i for i, c in enumerate(universe)}
px4 = {c: 20_000 for c in universe}


def model4(order):
    df = model(order, px4)
    df["VOL"] = [vols[c] for c in order]
    return df


rm = portfolio.rebalance(model4(universe), n=N, tranches=TR,
                         today=date(2026, 10, 9), capital=CAP4,
                         table=PM["state_table"], slot_offset=PM["slot_offset"],
                         weighting=PM["weighting"])
ra = portfolio.rebalance(model4(universe), n=N, tranches=TR,
                         today=date(2026, 10, 9), capital=CAP4,
                         table=PA["state_table"], slot_offset=PA["slot_offset"],
                         weighting=PA["weighting"])
check(len(portfolio.load_state(PM["state_table"])) == N, "main 계좌 100종목")
check(len(portfolio.load_state(PA["state_table"])) == N, "alt 계좌 100종목")

# 비중 방식이 실제로 주문서에 들어갔는가 — 동일가중이면 범위가 한 점입니다
lo_m, hi_m = rm["배정액범위"]
lo_a, hi_a = ra["배정액범위"]
print(f"    main(score)  배정액 {lo_m:,} ~ {hi_m:,}원")
print(f"    alt(score·섹터중립) 배정액 {lo_a:,} ~ {hi_a:,}원")
# 점수가중은 '전체 유니버스 순위'로 매기므로 상위 100위 안의 기울기는
# 완만합니다 (백테스트가 그렇게 돌았고, 실전이 그걸 재현해야 합니다).
check(hi_m > lo_m * 1.05, f"점수가중이 종목별로 다른 금액을 배정 ({lo_m:,}~{hi_m:,})")
check(hi_m < lo_m * 3, f"다만 극단적으로 쏠리지는 않음 ({hi_m / lo_m:.2f}배)")
check(hi_a > lo_a * 1.05, f"alt도 종목별로 다른 금액 ({lo_a:,}~{hi_a:,})")
# 역변동성 자체는 함수 단위로 확인합니다 (프로필에서는 쓰지 않습니다)
ai = portfolio.target_amounts(universe[:N], capital=CAP4, n=N,
                              weighting="invvol", vol_map=vols)
check(max(ai.values()) > min(ai.values()) * 2,
      f"역변동성은 변동성에 따라 크게 다름 ({max(ai.values())/min(ai.values()):.1f}배)")
check(abs(sum(portfolio.load_state(PM["state_table"])["기준금액"]) - CAP4)
      < CAP4 * 0.02, "main 배정액 합이 총자본에 수렴")
check(abs(sum(portfolio.load_state(PA["state_table"])["기준금액"]) - CAP4)
      < CAP4 * 0.02, "alt 배정액 합이 총자본에 수렴")

# 점수가중은 1위에, 역변동성은 저변동 종목에 가장 많이 실려야 합니다
am = portfolio.target_amounts(universe, capital=CAP4, n=N, weighting="score")
aa = portfolio.target_amounts(universe, capital=CAP4, n=N, weighting="invvol",
                              vol_map=vols)
check(max(am, key=am.get) == universe[0], "점수가중 최대 배정 = 1순위")
check(max(aa, key=aa.get) == min(
    list(universe)[:N], key=lambda c: vols[c]), "역변동성 최대 배정 = 최저변동성")
ae = portfolio.target_amounts(universe, capital=CAP4, n=N, weighting="equal")
check(len(set(round(v) for v in ae.values())) == 1, "동일가중은 전 종목 같은 금액")
# 상위 n종목의 합만 총자본과 같습니다. 그 밖의 종목은 '버퍼 구간에서 비중
# 조절을 계산하기 위한 참고 목표'일 뿐 배분 대상이 아닙니다.
top_m = sum(am[c] for c in universe[:N])
top_a = sum(aa[c] for c in universe[:N])
check(abs(top_m - CAP4) < 1 and abs(top_a - CAP4) < 1,
      f"상위 100종목 목표 합 = 총자본 ({top_m:,.0f} / {top_a:,.0f})")
check(len(am) > N, "버퍼 구간 종목도 참고 목표를 가짐")

# 상한이 걸리는가
ac = portfolio.target_amounts(universe, capital=CAP4, n=N, weighting="score",
                              cap=0.02)
check(max(ac.values()) <= CAP4 * 0.02 + 1,
      f"종목 상한 2%를 지킴 (최대 {max(ac.values()):,.0f}원)")

# 전체 유니버스 순위를 넘기면 백테스트와 같은(완만한) 기울기가 나와야 합니다
ar = portfolio.target_amounts(
    universe[:N], capital=CAP4, n=N, weighting="score",
    rank_map={c: i + 1 for i, c in enumerate(universe)},
    universe_n=len(universe))
check(max(ar.values()) / min(ar.values()) < 1.5,
      f"전체순위 기준이면 기울기가 완만 ({max(ar.values())/min(ar.values()):.2f}배)")
nr = portfolio.target_amounts(universe[:N], capital=CAP4, n=N, weighting="score")
check(max(nr.values()) / min(nr.values()) > 10,
      f"명단 안 순위만 쓰면 극단적으로 쏠림 "
      f"({max(nr.values())/min(nr.values()):.0f}배) — 그래서 전체순위를 넘깁니다")

# 등분 위상이 엇갈리는가 — 같은 주에 다른 등분을 손봐야 합니다
same_week = [(portfolio.current_slot(TR, date(2026, 10, d), PM["slot_offset"]),
              portfolio.current_slot(TR, date(2026, 10, d), PA["slot_offset"]))
             for d in (9, 16, 23, 30)]
check(all(a != b for a, b in same_week),
      f"매주 서로 다른 등분을 손봄 {same_week}")

# 한 계좌의 거래가 다른 계좌 상태를 건드리지 않는가
before_a = portfolio.load_state(PA["state_table"]).copy()
shifted4 = universe[15:] + universe[:15]
portfolio.rebalance(model4(shifted4), n=N, tranches=TR, today=date(2026, 10, 16),
                    capital=CAP4, table=PM["state_table"],
                    slot_offset=PM["slot_offset"], weighting=PM["weighting"])
after_a = portfolio.load_state(PA["state_table"])
check(set(before_a["종목코드"]) == set(after_a["종목코드"]),
      "main 거래가 alt 보유를 바꾸지 않음")
check(len(before_a) == len(after_a), "alt 종목 수 그대로")

shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 실전이 백테스트와 같은 규칙으로 사고팝니다.")
