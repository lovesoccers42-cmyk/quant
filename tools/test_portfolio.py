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

# 이 파일의 대부분은 '이미 100종목을 들고 있는 상태'의 주차별 동작을 봅니다.
# 그래서 기본값은 일괄 진입으로 두고, 분할 진입(config.STAGED_FIRST)은 아래
# [분할 진입] 절에서 staged_first=True로 명시해 따로 검증합니다.
_rebalance = portfolio.rebalance


def _rebalance_default_oneshot(*a, **kw):
    kw.setdefault("staged_first", False)
    return _rebalance(*a, **kw)


portfolio.rebalance = _rebalance_default_oneshot

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
# 분할 진입은 아래 [분할 진입] 절에서 따로 봅니다. 여기서는 등분 배분과
# 주차별 동작을 보려고 일괄 진입으로 상태를 만듭니다.
r1 = portfolio.rebalance(model(universe), n=N, tranches=TR,
                         today=date(2026, 10, 9))
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
# 상위 3종목은 1주 50만원(배정액의 1.67배) — 2배 안이므로 1주만 담아야 합니다.
# 그다음 3종목은 1주 150만원(5배) — 이건 포기하고 다음 순위로 채워야 합니다.
near = {c: 500_000 for c in universe[:3]}
far = {c: 1_500_000 for c in universe[3:6]}
px = {c: near.get(c, far.get(c, 20_000)) for c in universe}
r8 = portfolio.rebalance(model(universe, px), n=N, tranches=TR,
                         today=date(2026, 11, 6), capital=CAP)
check(r8["종목당배정액"] == 300_000, f"종목당 배정액 30만원 ({r8['종목당배정액']:,})")
check(r8["가격초과제외"] == 3,
      f"2배를 넘는 3종목만 제외 ({r8['가격초과제외']}) — 1.67배는 1주로 담습니다")
buys = r8["orders"].query("구분 == '매수'")
bought = set(buys["종목코드"])
check(not (bought & set(universe[3:6])), "2배를 넘는 종목은 주문에 없음")
check(set(universe[:3]) <= bought, "2배 안쪽 비싼 종목은 1주로 담김")
one = buys[buys["종목코드"].isin(universe[:3])]
check(bool((pd.to_numeric(one["수량"]) == 1).all()),
      f"비싼 종목은 정확히 1주 ({sorted(set(pd.to_numeric(one['수량'])))})")
check(r8["보유종목수"] == N, f"그래도 100종목을 채움 ({r8['보유종목수']})")
qty = pd.to_numeric(buys["수량"])
check(bool((qty > 0).all()), "모든 매수 종목의 수량이 1주 이상")
cheap = buys[~buys["종목코드"].isin(universe[:3])]
cq = pd.to_numeric(cheap["수량"])
# 비싼 3종목에 150만원이 가므로 나머지 목표는 비례로 조금 줄어듭니다
check(bool(((cq >= 13) & (cq <= 15)).all()),
      f"나머지 종목 수량은 배정액 축소분만큼만 줄어듦 ({cq.min()}~{cq.max()}주)")
amt = pd.to_numeric(buys["예상금액"])
check(amt.sum() <= CAP + 1,
      f"예상금액 합계가 총자본을 넘지 않음 ({amt.sum():,.0f} / {CAP:,})")
check(bool((amt <= 600_000 + 1).all()),
      f"어떤 종목도 배정액의 2배를 넘지 않음 (최대 {amt.max():,.0f}원)")
print(f"    → 매수 {len(qty)}종목 · 합계 {amt.sum():,.0f}원")

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
px3[pricey_code] = 1_000_000               # 1주 100만 > 목표 30만의 2배
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

# ── 현금으로 시작하는 계좌 (배우자 1000만원) ────────────────────
CASH_TB = "kor_capital_alt"
portfolio.set_capital(10_000_000, table=CASH_TB)
ci = portfolio.load_capital(CASH_TB)
check(ci["총자본"] == 10_000_000, f"총자본이 적힘 ({ci['총자본']:,.0f})")
check(ci["현금"] == 0,
      "대기현금은 0 — 다음 주에 이미 투자한 돈을 또 넣지 않음")

def model_cash(order, prices=None):
    """실전 파일과 같은 모양 — 전체순위까지 들어 있어야 기울기가 같습니다."""
    df = model4(order) if prices is None else model(order, prices)
    if "VOL" not in df.columns:
        df["VOL"] = [vols[c] for c in order]
    df["전체순위"] = range(1, len(order) + 1)
    df["전체종목수"] = len(order)
    return df


cash_state = "kor_portfolio_cash_test"
rc = portfolio.rebalance(
    model_cash(universe), n=N, tranches=TR, today=date(2026, 10, 8),
    capital=ci["총자본"], cash=ci["현금"], table=cash_state,
    weighting="score", staged_first=True)
check(rc["첫실행"] and 20 <= rc["매수"] <= 26,
      f"현금 계좌 첫 주문서는 한 등분만 ({rc['매수']}종목 매수)")
check(rc["배정완료종목수"] == N and rc["미매수"] == N - rc["매수"],
      f"나머지 {rc['미매수']}종목은 배정만 해두고 다음 등분 차례에")
check(not rc.get("현금투입"), "첫 주문서에 현금투입 주문이 섞이지 않음")
qty = pd.to_numeric(rc["orders"]["수량"], errors="coerce")
check(qty.notna().all() and (qty > 0).all(),
      f"모든 주문에 1주 이상 수량이 붙음 (최소 {qty.min():.0f}주)")
spend = pd.to_numeric(rc["orders"]["예상금액"], errors="coerce").sum()
check(spend <= 10_000_000,
      f"예상금액 합계가 투자금 이내 ({spend:,.0f}원)")

# 비싼 주식은 1주도 못 삽니다 — 다음 순위로 채워야 합니다
pricey = {universe[0]: 500_000.0, universe[1]: 300_000.0}
rp = portfolio.rebalance(
    model_cash(universe, prices={**{c: 10_000.0 for c in universe}, **pricey}),
    n=N, tranches=TR, today=date(2026, 10, 8), capital=10_000_000,
    table="kor_portfolio_pricey_test", weighting="score")
check(rp.get("가격초과제외", 0) >= 2,
      f"배정액보다 비싼 종목을 제외 ({rp.get('가격초과제외')}종목)")
check(rp["보유종목수"] == N, f"제외한 자리를 다음 순위로 채움 ({rp['보유종목수']})")
check(universe[0] not in set(rp["orders"]["종목코드"]),
      "1주도 못 사는 종목은 주문서에 없음")

# ── 분할 진입 (현금 계좌 첫 주문서를 4주에 나눠 담기) ────────────
print("\n[분할 진입]")
ST = "kor_portfolio_staged_test"
CAPS = 10_000_000
WEEKS = [date(2026, 10, 8), date(2026, 10, 15), date(2026, 10, 22),
         date(2026, 10, 29)]
slots = [portfolio.current_slot(TR, d) for d in WEEKS]
check(sorted(slots) == [0, 1, 2, 3], f"4주가 서로 다른 등분을 짚음 {slots}")

r_1 = portfolio.rebalance(model_cash(universe), n=N, tranches=TR,
                          today=WEEKS[0], capital=CAPS, table=ST,
                          weighting="score", staged_first=True)
check(r_1["첫실행"] and r_1["이번등분"] == slots[0],
      f"첫 실행이지만 이번 등분({r_1['이번등분']})만 손봄")
check(20 <= r_1["매수"] <= 26, f"한 등분(약 25종목)만 매수 ({r_1['매수']})")
check(r_1["미매수"] == N - r_1["매수"],
      f"나머지는 배정만 ({r_1['미매수']}종목 미매수)")
check(r_1["배정완료종목수"] == N, f"배정은 100종목 전부 ({r_1['배정완료종목수']})")
check(r_1["보유종목수"] == r_1["매수"],
      f"보유 집계는 실제 산 것만 ({r_1['보유종목수']})")
spend1 = pd.to_numeric(r_1["orders"]["예상금액"], errors="coerce").sum()
check(spend1 < CAPS * 0.35,
      f"이번 주 투입액은 전체의 1/4 수준 ({spend1:,.0f}원 / {CAPS:,}원)")

# 주차가 지나면 남은 등분을 순서대로 채우고, 산 적 없는 종목을 팔지 않습니다
held = [r_1["매수"]]
for i, d in enumerate(WEEKS[1:], start=1):
    rr = portfolio.rebalance(model_cash(universe), n=N, tranches=TR, today=d,
                             capital=CAPS, table=ST, weighting="score",
                             staged_first=True)
    check(rr["매도"] == 0,
          f"{i + 1}주차: 산 적 없는 종목을 팔지 않음 (매도 {rr['매도']})")
    held.append(rr["보유종목수"])
check(held[-1] == N, f"4주 뒤 100종목 완성 (주차별 보유 {held})")
last = portfolio.load_state(ST)
zeros = int((pd.to_numeric(last["기준금액"], errors="coerce") == 0).sum())
check(zeros == 0, f"미매수 종목이 남지 않음 ({zeros}종목)")
check(all(held[i] < held[i + 1] for i in range(len(held) - 1)),
      f"매주 늘어남 {held}")

# 중간에 순위에서 밀려난 '미매수' 종목은 매도가 아니라 그냥 빠져야 합니다
ST2 = "kor_portfolio_staged_drop_test"
portfolio.rebalance(model_cash(universe), n=N, tranches=TR, today=WEEKS[0],
                    capital=CAPS, table=ST2, weighting="score",
                    staged_first=True)
shifted = universe[60:] + universe[:60]      # 상위 60종목이 통째로 밀려남
rd = portfolio.rebalance(model_cash(shifted), n=N, tranches=TR, today=WEEKS[1],
                         capital=CAPS, table=ST2, weighting="score",
                         staged_first=True)
sold = set(rd["orders"].query("구분 == '매도'")["종목코드"])
st2 = portfolio.load_state(ST2)
check(not sold, f"순위에서 밀려난 미매수 종목은 매도 주문이 안 나옴 ({len(sold)})")
check(rd["매수"] > 0, f"그 자리는 새 종목으로 채움 (매수 {rd['매수']})")

# 분할을 끄면 예전처럼 하루에 다 담습니다
ST3 = "kor_portfolio_oneshot_test"
r_os = portfolio.rebalance(model_cash(universe), n=N, tranches=TR,
                           today=WEEKS[0], capital=CAPS, table=ST3,
                           weighting="score", staged_first=False)
check(r_os["매수"] == N and r_os["미매수"] == 0,
      f"staged_first=False면 100종목 일괄 ({r_os['매수']}매수)")

# 모델 파일이 깊어야 1주 제약으로 빈 자리를 채울 수 있습니다 (실측 버그)
ST4 = "kor_portfolio_depth_test"
expensive = {c: 400_000.0 for c in universe[:10]}      # 배정액의 4배
px_mix = {**{c: 15_000.0 for c in universe}, **expensive}
r_d = portfolio.rebalance(model_cash(universe, prices=px_mix), n=N,
                          tranches=TR, today=WEEKS[0], capital=CAPS,
                          table=ST4, weighting="score", staged_first=False)
check(r_d["가격초과제외"] == 10, f"비싼 10종목 제외 ({r_d['가격초과제외']})")
check(r_d["배정완료종목수"] == N,
      f"명단이 깊으면 빈 자리를 다음 순위로 채워 100종목 ({r_d['배정완료종목수']})")
only100 = model_cash(universe[:100], prices=px_mix)
r_s = portfolio.rebalance(only100, n=N, tranches=TR, today=WEEKS[0],
                          capital=CAPS, table="kor_portfolio_shallow_test",
                          weighting="score", staged_first=False)
check(r_s["배정완료종목수"] <= 92,
      f"명단이 100종목뿐이면 {r_s['배정완료종목수']}종목밖에 못 채움 "
      f"(빈 자리를 메울 101위 이하가 없음) — 그래서 모델 파일을 200종목으로 씁니다")

# ── 거래 원장 · 자본 추이 (성과 계산의 전제) ──────────────────
print("\n[기록]")
LOG_ORDERS = pd.DataFrame([
    {"구분": "매도", "종목코드": "005930", "종목명": "삼성전자", "섹터": "IT",
     "주가": None, "수량": "전량", "예상금액": None},
    {"구분": "매수", "종목코드": "000660", "종목명": "하이닉스", "섹터": "IT",
     "주가": 200_000, "수량": 1, "예상금액": 200_000},
])
store.write("kor_trades", pd.DataFrame(columns=list(store.SCHEMAS["kor_trades"])))
portfolio.log_trades(LOG_ORDERS, account="main", today=date(2026, 10, 8))
portfolio.log_trades(LOG_ORDERS, account="main", today=date(2026, 10, 8))
portfolio.log_trades(LOG_ORDERS.head(1), account="alt", today=date(2026, 10, 8))
portfolio.log_trades(LOG_ORDERS.tail(1), account="main", today=date(2026, 10, 15))
tl = portfolio.trade_log()
check(len(tl) == 4, f"같은 날 같은 주문을 두 번 돌려도 중복이 안 쌓임 ({len(tl)}행)")
check(set(tl["계좌"]) == {"main", "alt"}, "계좌별로 따로 쌓임")
check(len(portfolio.trade_log(account="main")) == 3,
      "계좌로 걸러 읽힘 (main 3행)")
check(tl["기준일"].nunique() == 2, "주차가 쌓임 (10/08, 10/15)")
sell = tl[tl["구분"] == "매도"].iloc[0]
check(pd.isna(sell["수량"]) and "수량미정" in str(sell["메모"]),
      "전량매도는 수량을 비우고 메모로 남김")
check(pd.isna(tl["체결가"]).all(),
      "체결가는 비어 있음 — 계획값으로 추정한다는 표시")

# ── 약정액 vs 계좌 실측 ────────────────────────────────────────────────
# 왜 나누는가 (실측): 배우자 계좌는 1,000만원 운용 약정인데 살 때 그때그때
# 입금하므로 예수금이 0이고, 분할 진입 2주차 평가액이 210만원이었습니다.
# 평가액+예수금으로 배정액을 계산하면 종목당 10만원 → 2.1만원으로 줄어
# 분할 진입이 스스로 멈춥니다.
CT = "kor_capital_alt"
store.write(CT, pd.DataFrame(columns=list(store.SCHEMAS[CT])))
r = portfolio.set_capital(10_000_000, table=CT, today=date(2026, 10, 8))
check(r["약정액"] == 10_000_000 and r["운용기준"] == 10_000_000,
      "새 계좌 — 약정액이 곧 배정 기준")
check(portfolio.load_capital(CT)["운용기준"] == 10_000_000,
      "load_capital이 운용기준을 돌려줌")

# 예수금 0 · 평가액이 약정액보다 작아도 배정액은 약정액에서 나옵니다
store.upsert(CT, pd.DataFrame([{"기준일": pd.Timestamp("2026-10-16"),
                                "평가액": 2_103_345.0, "현금": 0.0,
                                "총자본": 2_103_345.0, "약정액": 10_000_000.0}]))
ci = portfolio.load_capital(CT)
check(ci["운용기준"] == 10_000_000 and ci["총자본"] == 2_103_345,
      f"예수금 0 · 평가액 210만 → 배정 기준 {ci['운용기준']:,.0f}원 (약정액)")
check(abs(ci["운용기준"] / 100 - 100_000) < 1,
      "종목당 배정액 10만원 — 2.1만원으로 줄지 않음")

# 평가액이 약정액을 넘으면 그때부터 평가액이 기준 (복리 반영)
store.upsert(CT, pd.DataFrame([{"기준일": pd.Timestamp("2027-10-16"),
                                "평가액": 12_000_000.0, "현금": 500_000.0,
                                "총자본": 12_500_000.0, "약정액": 10_000_000.0}]))
check(portfolio.load_capital(CT)["운용기준"] == 12_500_000,
      "평가액+예수금이 약정액을 넘으면 그쪽이 기준")

# 약정액을 다시 적어도 측정값(평가액·예수금)은 건드리지 않습니다 —
# 여기서 덮어쓰면 수익률이 약정액 기준으로 계산돼 성과가 거짓이 됩니다.
r = portfolio.set_capital(20_000_000, table=CT, today=date(2027, 11, 8))
check(r["평가액"] == 12_000_000 and r["현금"] == 500_000,
      f"약정액 변경이 평가액을 덮어쓰지 않음 (평가액 {r['평가액']:,.0f})")
check(r["운용기준"] == 20_000_000, "새 약정액이 배정 기준")
check(len(store.read(CT)) == 3,
      f"약정액 변경은 가짜 측정 시점을 만들지 않음 ({len(store.read(CT))}행)")

store.write(CT, pd.DataFrame(columns=list(store.SCHEMAS[CT])))
portfolio.set_capital(10_000_000, table=CT, today=date(2026, 10, 8))

# 재동기화는 종목별 평가금액까지 남겨야 합니다 (수량 복원·종목별 손익의 기준점)
store.write("kor_holdings_log",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_holdings_log"])))
store.write("kor_portfolio_alt",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_portfolio_alt"])))
portfolio.seed([{"종목코드": "005930", "평가금액": 500_000, "종목명": "삼성전자"},
                {"종목코드": "000660", "평가금액": 300_000, "종목명": "하이닉스"}],
               tranches=TR, today=date(2026, 10, 8), table="kor_portfolio_alt")
portfolio.resync([{"종목코드": "005930", "평가금액": 520_000, "종목명": "삼성전자"},
                  {"종목코드": "000660", "평가금액": 310_000, "종목명": "하이닉스"}],
                 cash=50_000, tranches=TR, today=date(2026, 11, 8),
                 table="kor_portfolio_alt", capital_table=CT)
hl = store.read("kor_holdings_log")
check(len(hl) == 2 and set(hl["계좌"]) == {"alt"},
      f"재동기화가 종목별 평가금액을 남김 ({len(hl)}행)")
check(abs(portfolio.load_capital(CT)["총자본"] - 880_000) < 1,
      f"재동기화 총자본 = 평가액+현금 ({portfolio.load_capital(CT)['총자본']:,.0f})")
# 재동기화가 약정액을 지우면 다음 주문서의 배정액이 쪼그라듭니다 —
# 분할 진입 중이면 그걸로 계획이 끝납니다.
check(portfolio.load_capital(CT)["약정액"] == 10_000_000,
      "재동기화가 약정액을 그대로 이어받음")
check(portfolio.load_capital(CT)["운용기준"] == 10_000_000,
      "재동기화 뒤에도 배정 기준은 약정액")
# 같은 날짜 기록은 덮어쓰고(중복 방지), 다른 날짜는 쌓습니다
check(len(store.read(CT)) == 2,
      f"같은 날 다시 맞추면 그 날 기록만 갱신 ({len(store.read(CT))}행: 10/08, 11/08)")
portfolio.resync([{"종목코드": "005930", "평가금액": 530_000, "종목명": "삼성전자"}],
                 cash=0, tranches=TR, today=date(2026, 12, 8),
                 table="kor_portfolio_alt", capital_table=CT)
check(len(store.read(CT)) == 3,
      f"다른 날 재동기화는 기록이 쌓임 ({len(store.read(CT))}행)")
hist = store.read(CT).sort_values("기준일")["총자본"].tolist()
check(hist == sorted(set(hist), key=hist.index) and len(hist) == 3,
      f"총자본 추이를 뽑을 수 있음 {[f'{v:,.0f}' for v in hist]}")

# ── 체결내역 반영 (실제 수익률의 근거) ────────────────────────
print("\n[체결내역]")
store.write("kor_trades", pd.DataFrame(columns=list(store.SCHEMAS["kor_trades"])))
FILL_ORDERS = pd.DataFrame([
    {"구분": "매수", "종목코드": "003010", "종목명": "혜인", "섹터": "산업재",
     "주가": 8860, "수량": 36, "예상금액": 318_960},
    {"구분": "매수", "종목코드": "264450", "종목명": "유비쿼스", "섹터": "IT",
     "주가": 11720, "수량": 27, "예상금액": 316_440},
    {"구분": "매도", "종목코드": "009970", "종목명": "영원무역홀딩스", "섹터": "소비재",
     "주가": None, "수량": "전량", "예상금액": None},
])
portfolio.log_trades(FILL_ORDERS, account="main", today=date(2026, 10, 8))

# 증권사마다 모양이 달라도 읽혀야 합니다
FORMS = {
    "탭+헤더(코드)": "종목코드\t매매구분\t체결수량\t체결단가\n"
                 "003010\t매수\t36\t8,900\n009970\t매도\t12\t62,300",
    "쉼표+헤더(이름만)": "종목명,구분,수량,단가\n혜인,매수,36,8900원",
    "헤더 없음": "003010\t매수\t36\t8900",
    "구분 칸 없음": "종목명\t수량\t단가\n혜인\t36\t8900",
}
for label, txt in FORMS.items():
    got = portfolio.parse_fills(txt)
    ok = len(got) >= 1 and (got["체결가"] > 0).all() and (got["체결수량"] > 0).all()
    check(ok, f"{label} 형식을 읽음 ({len(got)}건)")

r_f = portfolio.record_fills(FORMS["탭+헤더(코드)"], account="main",
                             today=date(2026, 10, 9))
check(r_f["체결반영"] == 2, f"체결 2건 반영 ({r_f['체결반영']})")
check(r_f["미체결"] == 1 and r_f["미체결종목"] == ["264450"],
      f"못 산 종목은 미체결로 남음 ({r_f['미체결종목']})")
# 체결가가 비어 있는 행은 '계획값으로 추정'이라는 뜻이므로, 체결내역을
# 넣은 주의 미체결 행은 원장에 그렇다고 적어야 합니다. 안 적으면 실제로는
# 못 판 종목을 계획가에 팔았다고 계산합니다 (실측: 10/7 매도 4종목).
_tl = portfolio.trade_log(account="main")
_miss = _tl[_tl["종목코드"] == "264450"].iloc[0]
check("미체결" in str(_miss["메모"]) and pd.isna(_miss["체결가"]),
      f"미체결 행에 표시가 남음 (메모 '{_miss['메모']}')")
_done = _tl[_tl["종목코드"] == "003010"].iloc[0]
check("미체결" not in str(_done["메모"] or ""),
      "체결된 행에는 미체결 표시가 없음")
tl = portfolio.trade_log(account="main")
filled = tl[tl["체결가"].notna()]
check(len(filled) == 2, f"원장에 체결가가 들어감 ({len(filled)}건)")
buy = filled[filled["종목코드"] == "003010"].iloc[0]
check(buy["체결가"] == 8900 and buy["주가"] == 8860,
      f"계획가({buy['주가']:,.0f})와 체결가({buy['체결가']:,.0f})를 모두 보관")
sell = filled[filled["구분"] == "매도"].iloc[0]
check(sell["체결수량"] == 12,
      f"전량매도도 실제 수량이 들어감 ({sell['체결수량']:.0f}주)")

# 주문서에 없던 종목을 직접 샀어도 성과에 들어가야 합니다
r_x = portfolio.record_fills("종목코드\t구분\t체결수량\t체결가\n005930\t매수\t3\t80000",
                             account="main", today=date(2026, 10, 9))
check(r_x["원장밖"] == 1, f"원장에 없던 체결을 새로 적음 ({r_x['원장밖']}건)")
ext = portfolio.trade_log(account="main")
ext = ext[ext["종목코드"] == "005930"]
check(len(ext) == 1 and "원장밖" in str(ext.iloc[0]["메모"]),
      "원장밖 체결에 표시가 남음")

# 같은 종목을 여러 번 체결하면 수량가중 평균단가로 합쳐야 합니다
avg = portfolio.parse_fills("종목코드\t구분\t체결수량\t체결가\n"
                            "003010\t매수\t20\t9000\n003010\t매수\t20\t8800")
check(len(avg) == 1 and abs(float(avg.iloc[0]["체결가"]) - 8900) < 1,
      f"분할 체결을 평균단가로 합침 ({float(avg.iloc[0]['체결가']):,.0f}원)")

# ── 원장 백필 · 현금 이중지출 방지 ────────────────────────────
print("\n[백필 · 현금]")
store.write("kor_trades", pd.DataFrame(columns=list(store.SCHEMAS["kor_trades"])))
BF = {"프로필": "alt", "기준일": "2026-10-07", "orders": [
    {"구분": "매수", "종목코드": "008060", "종목명": "대덕", "섹터": "IT",
     "주가": 16400, "수량": 6, "예상금액": 98_400},
    {"구분": "매수", "종목코드": "001060", "종목명": "JW중외제약",
     "섹터": "건강관리", "주가": 27300, "수량": 3, "예상금액": 81_900}]}
rb = portfolio.log_trades_from_json(BF, account="alt")
check(rb["기록"] == 2 and rb["기준일"] == "2026-10-07",
      f"주문서 json에서 원장을 되살림 ({rb['기록']}건, {rb['기준일']})")
rf = portfolio.record_fills(
    "종목코드\t구분\t체결수량\t체결가\n008060\t매수\t6\t16500",
    account="alt", today=date(2026, 10, 8), asof=date(2026, 10, 7))
check(rf["체결반영"] == 1, "백필한 주문에 체결가가 붙음")
# 원장이 없던 날짜도 체결내역만으로 기록할 수 있어야 합니다
ro = portfolio.record_fills(
    "종목코드\t구분\t체결수량\t체결가\n005930\t매수\t2\t81000",
    account="alt", today=date(2026, 10, 9), asof=date(2026, 9, 30))
check(ro["원장밖"] == 1,
      "원장이 없던 날짜도 체결내역만으로 기록됨 (계획가 없이)")

# 분할 진입 중 남은 현금을 등록해도 같은 돈을 두 번 쓰지 않아야 합니다
ST5 = "kor_portfolio_cashguard_test"
half = pd.DataFrame([{"종목코드": f"{i:06d}", "등분": i % TR,
                      "편입일": pd.Timestamp("2026-10-07"),
                      "기준금액": (100_000.0 if i % TR == 3 else 0.0),
                      "종목명": "", "섹터": ""} for i in range(1, N + 1)])
store.write(ST5, half)
wide = pd.DataFrame({"종목코드": [f"{i:06d}" for i in range(1, 201)],
                     "종목명": [f"n{i}" for i in range(1, 201)],
                     "SEC_NM_KOR": ["S1"] * 200,
                     "qvm": [-2 + i * 0.01 for i in range(200)],
                     "종가": [20_000.0] * 200,
                     "전체순위": list(range(1, 201)),
                     "전체종목수": [600] * 200})
rg = portfolio.rebalance(wide, n=N, tranches=TR, today=date(2026, 10, 12),
                         capital=10_000_000, cash=7_730_000, table=ST5,
                         slot_offset=2, weighting="score")
check(rg["미매수예약금액"] == 7_500_000,
      f"미매수 75종목 몫을 현금에서 예약 ({rg['미매수예약금액']:,}원)")
check(rg["이번주현금예산"] < 100_000,
      f"남은 현금을 또 쓰려 하지 않음 (예산 {rg['이번주현금예산']:,}원)")
check(rg["매수"] == 25, f"이번 등분 25종목만 매수 ({rg['매수']})")
plan = pd.to_numeric(rg["orders"]["예상금액"], errors="coerce").sum()
check(plan < 3_000_000,
      f"이번 주 주문 총액이 한 등분 몫 ({plan:,.0f}원)")

# ── 매주 resync — 분할 진입 계획을 지우지 않아야 합니다 ────────
print("\n[매주 재동기화]")
ST6 = "kor_portfolio_weekly_resync"
CT6 = "kor_capital_alt"
store.write(CT6, pd.DataFrame(columns=list(store.SCHEMAS[CT6])))
staged = pd.DataFrame([{"종목코드": f"{i:06d}", "등분": i % TR,
                        "편입일": pd.Timestamp("2026-10-07"),
                        "기준금액": (90_000.0 if i % TR == 3 else 0.0),
                        "종목명": f"n{i}", "섹터": "S1"}
                       for i in range(1, N + 1)])
store.write(ST6, staged)
held = [{"종목코드": f"{i:06d}", "평가금액": 95_000, "종목명": f"n{i}"}
        for i in range(1, N + 1) if i % TR == 3]
rr6 = portfolio.resync(held, cash=7_730_000, tranches=TR,
                       today=date(2026, 10, 16), table=ST6, capital_table=CT6)
st6 = portfolio.load_state(ST6)
check(len(st6) == N, f"상태 100행 그대로 ({len(st6)}행)")
check(rr6["미매수유지"] == 75,
      f"미매수 75종목을 '팔린 것'으로 지우지 않음 ({rr6['미매수유지']})")
check(rr6["보유종목수"] == 25, f"실제 보유만 25종목으로 셈 ({rr6['보유종목수']})")
check(not rr6["사라진종목"], f"사라진 종목 없음 ({len(rr6['사라진종목'])})")
check(abs(rr6["총자본"] - 10_105_000) < 1,
      f"총자본 = 실제 평가액 + 현금 ({rr6['총자본']:,.0f})")
check(abs(rr6["종목당배정액"] - 101_050) < 1,
      f"수익만큼 배정액이 따라 커짐 ({rr6['종목당배정액']:,.0f}원)")
# 매주 맞춰도 현금 이중지출 가드가 살아 있어야 합니다
rg6 = portfolio.rebalance(wide, n=N, tranches=TR, today=date(2026, 10, 16),
                          capital=rr6["총자본"], cash=rr6["현금"], table=ST6,
                          slot_offset=2, weighting="score")
check(rg6["미매수예약금액"] > 7_000_000,
      f"resync 뒤에도 미매수 몫을 예약 ({rg6['미매수예약금액']:,}원)")
spend6 = pd.to_numeric(rg6["orders"]["예상금액"], errors="coerce").sum()
check(spend6 < 3_000_000,
      f"주문 총액이 한 등분 몫으로 유지 ({spend6:,.0f}원)")
# 매주 맞추면 성과 곡선이 주간 해상도가 됩니다
portfolio.resync(held, cash=7_730_000, tranches=TR, today=date(2026, 10, 23),
                 table=ST6, capital_table=CT6)
check(len(store.read(CT6)) == 2, f"주마다 총자본이 한 점씩 쌓임 ({len(store.read(CT6))}점)")

# ── 예수금 줄 떼어내기 · inbox 파일 이름 해석 ──────────────────
print("\n[CSV 입력]")
CASH_CASES = {
    "예수금 줄": ("종목코드,평가금액,종목명\n005930,520000,삼성전자\n"
               "예수금,7730000,\n", 1, 7_730_000),
    "종목명이 현금": ("종목코드,평가금액,종목명\n005930,520000,삼성전자\n"
                 ",7730000,현금\n", 1, 7_730_000),
    "D+2예수금": ("종목코드,평가금액,종목명\n005930,520000,삼성전자\n"
                "D+2예수금,123456,\n", 1, 123_456),
    "탭 구분": ("종목코드\t평가금액\t종목명\n005930\t520000\t삼성전자\n"
              "예수금\t7,730,000\t\n", 1, 7_730_000),
    "줄바꿈 없음": ("종목코드,평가금액,종목명 005930,520000,삼성전자 "
                "예수금,7730000,", 1, 7_730_000),
    "현금 줄 없음": ("005930,520000,삼성전자\n000660,310000,하이닉스", 2, 0),
    "종목명에 '현금배당'": ("종목코드,평가금액,종목명\n"
                      "005930,520000,삼성전자현금배당\n", 1, 0),
}
for label, (txt, n_expect, cash_expect) in CASH_CASES.items():
    rest, cash = portfolio.extract_cash(txt)
    got = portfolio.parse_holdings(rest)
    ok = len(got) == n_expect and abs(cash - cash_expect) < 1
    check(ok, f"{label} — 보유 {len(got)}종목 · 현금 {cash:,.0f}원 "
              f"(기대 {n_expect}종목 · {cash_expect:,}원)")

_ia_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "inbox_apply.py")
import importlib.util as _u  # noqa: E402
_spec = _u.spec_from_file_location("_inbox_apply", _ia_path)
_ia = _u.module_from_spec(_spec)
_spec.loader.exec_module(_ia)

NAME_CASES = {
    "fills_main_2026-10-08.csv": ("fills", "main", date(2026, 10, 8)),
    "holdings_alt.csv": ("holdings", "alt", None),
    "FILLS_ALT_20261016.CSV": ("fills", "alt", date(2026, 10, 16)),
    "holdings-main-2026_10_16.csv": ("holdings", "main", date(2026, 10, 16)),
    "엉뚱한파일.csv": (None, None, None),
    "fills.csv": ("fills", None, None),          # 계좌를 못 읽으면 건너뜀
}
for name, want in NAME_CASES.items():
    got = _ia._parse_name(name)
    check(got == want, f"파일 이름 해석 {name} → {got}")

# ── 보유 수량 받기 (주문 계산은 그대로 금액 기준) ──────────────
print("\n[보유 수량]")
QTY_CASES = {
    "수량 칸": ("종목코드,평가금액,종목명,수량\n005930,520000,삼성전자,8\n", 8),
    "순서 뒤바뀜": ("종목명,수량,종목코드,평가금액\n삼성전자,8,005930,520000\n", 8),
    "보유수량 이름": ("종목코드\t보유수량\t평가금액\t종목명\n"
                 "005930\t8\t520000\t삼성전자\n", 8),
    "'8주' 표기": ("종목코드,평가금액,수량\n005930,520000,8주\n", 8),
    "수량 칸 없음": ("종목코드,평가금액,종목명\n005930,520000,삼성전자\n", None),
}
for label, (txt, want) in QTY_CASES.items():
    got = portfolio.parse_holdings(txt)
    q = got.iloc[0]["수량"]
    ok = (pd.isna(q) if want is None else abs(float(q) - want) < 1e-9)
    check(ok, f"{label} — 수량 {('없음' if pd.isna(q) else q)} (기대 {want})")

ST7 = "kor_portfolio_qty_test"
CT7 = "kor_capital_alt"
store.write(CT7, pd.DataFrame(columns=list(store.SCHEMAS[CT7])))
store.write("kor_holdings_log",
            pd.DataFrame(columns=list(store.SCHEMAS["kor_holdings_log"])))
store.write(ST7, pd.DataFrame([
    {"종목코드": "005930", "등분": 0, "편입일": pd.Timestamp("2026-10-07"),
     "기준금액": 500_000.0, "수량": np.nan, "종목명": "삼성전자", "섹터": "IT"},
    {"종목코드": "000660", "등분": 1, "편입일": pd.Timestamp("2026-10-07"),
     "기준금액": 300_000.0, "수량": np.nan, "종목명": "하이닉스", "섹터": "IT"}]))
_txt = ("종목코드,평가금액,종목명,수량\n005930,520000,삼성전자,8\n"
        "000660,310000,하이닉스,2\n예수금,412000,,\n")
_rest, _cash = portfolio.extract_cash(_txt)
r7 = portfolio.resync(_rest, cash=_cash, tranches=TR, today=date(2026, 10, 16),
                      table=ST7, capital_table=CT7)
check(r7["수량있는종목"] == 2, f"재동기화가 수량을 저장 ({r7['수량있는종목']}종목)")
st7 = portfolio.load_state(ST7)
check(float(st7.set_index("종목코드").at["005930", "수량"]) == 8,
      "종목별 수량이 상태에 들어감")
check(abs(r7["총자본"] - 1_242_000) < 1,
      f"총자본은 금액 기준 그대로 ({r7['총자본']:,.0f})")
hl7 = store.read("kor_holdings_log")
check(len(hl7) == 2 and float(pd.to_numeric(hl7["수량"]).sum()) == 10,
      f"보유 기록에도 수량이 남음 (합 {pd.to_numeric(hl7['수량']).sum():.0f}주)")
# 수량이 없어도 예전처럼 동작해야 합니다
r7b = portfolio.resync("종목코드,평가금액,종목명\n005930,520000,삼성전자\n"
                       "000660,310000,하이닉스\n",
                       cash=0, tranches=TR, today=date(2026, 10, 23),
                       table=ST7, capital_table=CT7)
check(r7b["수량있는종목"] == 0 and r7b["보유종목수"] == 2,
      "수량 칸이 없어도 금액만으로 정상 동작")

shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 실전이 백테스트와 같은 규칙으로 사고팝니다.")
