# -*- coding: utf-8 -*-
"""미국 실전 운용 배관 점검 — 티커가 숫자가 아니어도 망가지지 않는가.

portfolio.py는 원래 한국 전용이었습니다. 종목코드를 "앞자리 0을 살린 6자리
숫자"로 다루는 자리가 아홉 군데 있었고, 거기에 티커가 들어가면

    zfill(6)                → 'AAPL'  → '00AAPL'   (없는 종목이 됩니다)
    str.replace(r'\\D','')   → 'AAPL'  → ''         (통째로 사라집니다)

가 됩니다. 한 군데만 빠뜨려도 주문서는 멀쩡해 보이는데 보유 상태와 원장이
서로 다른 코드를 쓰게 되고, 그러면 '산 적 없는 종목을 팔라'는 주문이 나옵니다.
실제 돈이 들어가므로 여기서 못 박습니다.

그리고 한국 경로가 한 글자도 달라지지 않았는지도 같이 봅니다 — 이 변경의
조건입니다.
"""
import os
import sys
import tempfile
from datetime import date

os.environ["QUANT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import portfolio as pf  # noqa: E402
import store  # noqa: E402

FAIL = 0
ST, CAP, TR = "us_portfolio", "us_capital", "us_trades"


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def picks(symbols, prices, sectors=None):
    sectors = sectors or ["Tech"] * len(symbols)
    return pd.DataFrame({"종목코드": symbols,
                         "종목명": [s + " Inc" for s in symbols],
                         "SEC_NM_KOR": sectors,
                         "종가": prices,
                         "qvm": range(len(symbols))})


# ── 코드 정규화 ──────────────────────────────────────────────
print("\n[종목코드 정규화]")
pf.set_market("kr")
check(pf._code("70") == "000070", f"한국은 6자리 0채움 ({pf._code('70')})")
check(pf._code("005930") == "005930", "한국 정상 코드는 그대로")

pf.set_market("us")
for raw, want in (("aapl", "AAPL"), (" msft ", "MSFT"), ("BRK.B", "BRK.B"),
                  ("BF-B", "BF-B")):
    got = pf._code(raw)
    check(got == want, f"미국 티커 {raw!r} → {got!r} (기대 {want!r})")
check(pf._code("AAPL") != "00AAPL", "zfill이 티커를 망가뜨리지 않음")

print("\n[테이블 가드 — 시장을 섞으면 멈춘다]")
for mkt, tbl in (("us", "kor_portfolio"), ("kr", "us_portfolio")):
    pf.set_market(mkt)
    try:
        pf.load_state(tbl)
        check(False, f"{mkt}인데 {tbl}을 그냥 읽음 — 막아야 합니다")
    except ValueError:
        check(True, f"{mkt} 러너가 {tbl}에 손대면 멈춤")

# ── 미국 주문 한 바퀴 ────────────────────────────────────────
pf.set_market("us")
SYMS = ["AAPL", "MSFT", "NVDA", "BRK.B", "JNJ", "KO", "XOM", "JPM"]
PX = [180.0, 400.0, 120.0, 350.0, 150.0, 60.0, 110.0, 200.0]

print("\n[첫 주문 — 보유 0에서]")
pf.reset_state(ST)
res = pf.rebalance(picks(SYMS, PX), n=8, tranches=4, capital=16_000.0,
                   today=date(2026, 10, 30), table=ST)
orders = res["orders"]
check(len(orders) > 0, f"주문이 나옴 ({len(orders)}건)")
codes = set(orders["종목코드"].astype(str))
check(codes <= set(SYMS), f"주문 종목코드가 전부 티커 모양 ({sorted(codes)})")
check(not any(c.startswith("00") for c in codes), "'00AAPL' 같은 코드가 없음")
qty = pd.to_numeric(orders["수량"], errors="coerce").dropna()
check(bool((qty == qty.astype(int)).all()) and bool((qty > 0).all()),
      f"수량이 전부 양의 정수 (소수점 매매 안 함) — {list(qty)}")

print("\n[상태 저장과 되읽기]")
held = pf.load_state(ST)
check(len(held) > 0, f"보유 상태에 저장됨 ({len(held)}종목)")
check(set(held["종목코드"]) <= set(SYMS),
      f"되읽은 코드도 티커 그대로 ({sorted(held['종목코드'])[:3]}...)")

print("\n[원장]")
n = pf.log_trades(orders, account="main", today=date(2026, 10, 30), table=TR)
check(n == len(orders), f"원장에 {n}건 기록")
log = store.read(TR)
check(set(log["종목코드"]) <= set(SYMS), "원장 코드도 티커 그대로")
check(set(log["종목코드"]) == codes, "주문서와 원장의 코드가 같음")

print("\n[체결 입력 — 브로커가 티커로 줄 때]")
# 주문에 없던 MSFT도 섞어 봅니다 — 직접 사신 종목은 '원장밖'으로 들어가야
# 하고(성과에 포함되어야 하니까요), 주문했는데 안 들어온 건 '미체결'입니다.
fills = pd.DataFrame({
    "종목코드": ["AAPL", "MSFT"],
    "종목명": ["Apple", "Microsoft"],
    "구분": ["매수", "매수"],
    "체결가": [181.5, 398.0],
    "체결수량": [11, 5],
})
try:
    got = pf.record_fills(fills, account="main", today=date(2026, 10, 31),
                          table=TR)
    check(int(got.get("체결반영", 0)) == 1,
          f"주문했던 AAPL 체결이 원장에 채워짐 ({got.get('체결반영')}건)")
    check(int(got.get("원장밖", 0)) == 1,
          f"주문에 없던 MSFT는 '원장밖'으로 들어감 ({got.get('원장밖')}건)")
    check(got.get("미체결종목") == ["JNJ"],
          f"주문했는데 안 들어온 JNJ는 미체결 ({got.get('미체결종목')})")
    after = store.read(TR)
    f_ok = after[after["종목코드"] == "AAPL"]["체결가"].notna().any()
    check(bool(f_ok), "AAPL 체결가가 채워짐")
    check("MSFT" in set(after["종목코드"].astype(str)),
          "원장밖 MSFT도 티커 그대로 원장에 남음")
except Exception as e:                                     # noqa: BLE001
    check(False, f"체결 반영이 예외로 죽음: {type(e).__name__} {e}")

print("\n[보유 목록 파싱 — 티커가 사라지지 않는가]")
hold_csv = ("종목코드,평가금액,종목명,수량\n"
            "AAPL,1996.5,Apple,11\n"
            "BRK.B,1750,Berkshire B,5\n"
            "KO,1200,Coca-Cola,20\n")
got = pf.parse_holdings(hold_csv)
check(len(got) == 3, f"3종목 모두 살아남음 ({len(got)}종목)")
check(set(got["종목코드"]) == {"AAPL", "BRK.B", "KO"},
      f"티커 그대로 ({sorted(got['종목코드'])})")
check(abs(float(got["평가금액"].sum()) - 4946.5) < 1e-6,
      f"평가금액 합 {float(got['평가금액'].sum()):,.1f}")

print("\n[재동기화]")
rs = pf.resync(hold_csv, cash=500.0, tranches=4, today=date(2026, 11, 1),
               table=ST, capital_table=CAP)
check(rs is not None, f"재동기화 동작 ({rs})")
capd = pf.load_capital(CAP)
check(float(capd.get("총자본", 0)) > 0,
      f"총자본이 들어감 ({capd.get('총자본')})")

print("\n[1주 가격이 배정액보다 비싸면 — 미국에서 더 자주 걸립니다]")
# 한국 규칙 그대로입니다: 1주 값이 배정액의 LOT_TOLERANCE배를 넘으면
# 후보에서 빼고 다음 순위로 채웁니다. 미국은 주가가 $6~$780으로 흩어져
# 있어서(유동성 통과 2,548종목 실측 중앙값 $40) 이 규칙이 한국보다
# 훨씬 자주 걸립니다 — 자본이 작으면 비싼 종목이 통째로 빠집니다.
import config as _cfg
TOL = float(_cfg.LOT_TOLERANCE)
pf.reset_state(ST)
r2 = pf.rebalance(picks(SYMS, PX), n=8, tranches=1, capital=1_600.0,
                  today=date(2026, 10, 30), table=ST)
bought = set(r2["orders"]["종목코드"].astype(str))
alloc = 1_600.0 / 8
too_dear = {s for s, px_ in zip(SYMS, PX) if px_ > alloc * TOL}
check(not (bought & too_dear),
      f"배정액 ${alloc:.0f}의 {TOL:g}배를 넘는 종목은 안 삼 "
      f"(제외 대상 {sorted(too_dear)} · 산 종목 {sorted(bought)})")
check("KO" in bought, "배정액 안에 들어오는 KO는 삼")
check(int(r2.get("가격초과제외", 0)) >= 0,
      f"가격초과제외가 요약에 보고됨 ({r2.get('가격초과제외')}종목)")

print("\n[한국 경로가 그대로인가 — 회귀]")
pf.set_market("kr")
pf.reset_state("kor_portfolio")
kr = pf.rebalance(
    pd.DataFrame({"종목코드": ["005930", "70", "000660"],
                  "종목명": ["삼성전자", "삼양홀딩스", "SK하이닉스"],
                  "SEC_NM_KOR": ["전기전자"] * 3,
                  "종가": [70_000.0, 60_000.0, 200_000.0],
                  "qvm": [0, 1, 2]}),
    n=3, tranches=1, capital=3_000_000.0, today=date(2026, 10, 30),
    table="kor_portfolio")
kc = set(kr["orders"]["종목코드"].astype(str))
check(kc <= {"005930", "000070", "000660"},
      f"한국은 여전히 6자리 0채움 ({sorted(kc)})")
check("000070" in kc, "'70'이 '000070'으로 복원됨")


# ── 목표액까지 단계 인상 ──────────────────────────────────────
print("\n[목표액 램프 — 전환을 하루에 몰지 않기]")
# 왜: 약정 $30,162를 한 번에 켜면 첫 주문서가 95건(42종목 신규매수 +
# 31종목 비중확대 + 22종목 비중축소, 추가 투입 $16,449)이 됩니다.
# 백테스트는 119번의 진입 시점에 흩어져 있어 하루 운을 평균내지만,
# 전환은 그 평균 안에 없는 일회성 사건입니다.
pf.set_market("us")
RT = "us_capital"
store.write(RT, pd.DataFrame(columns=list(store.SCHEMAS[RT])))
pf.set_capital(19_631.65, table=RT, today=date(2026, 10, 21))
r0 = pf.set_capital(30_162.0, table=RT, today=date(2026, 10, 21), ramp=True)
check(abs(float(r0["약정액"]) - 19_631.65) < 1,
      f"0개월차는 지금 돈 그대로 (${float(r0['약정액']):,.0f})")
check(abs(float(r0.get("목표액", 0)) - 30_162.0) < 1,
      f"목표액은 따로 기록 (${float(r0.get('목표액', 0)):,.0f})")

_row = pd.Series({"램프시작일": pd.Timestamp("2026-10-21"),
                  "램프시작액": 19_631.65})
want = {0: 19_632, 1: 23_142, 2: 26_652, 3: 30_162, 6: 30_162}
for m, w in want.items():
    d = pd.Timestamp("2026-10-21") + pd.DateOffset(months=m)
    got = pf._ramp(_row, 30_162.0, today=d.date())["적용약정"]
    check(abs(got - w) < 2, f"{m}개월차 ${got:,.0f} (기대 ${w:,.0f})")

print("\n[같은 달에 여러 번 돌려도 같은 값 — 날짜의 순수 함수]")
vals = {pf._ramp(_row, 30_162.0, today=date(2026, 11, d))["적용약정"]
        for d in (1, 10, 21, 30)}
check(len(vals) == 1, f"11월 안에서는 항상 같은 값 ({vals})")

print("\n[재동기화가 램프를 지우지 않는가 — 놓치기 쉬운 자리]")
# load_capital이 돌려주는 '약정액'은 램프가 적용된 이번 달 값입니다.
# resync가 그걸 그대로 다시 저장하면 다음 달 램프가 더 작은 값에서
# 시작해 목표액에 영영 도달하지 못합니다.
pf.reset_state(ST)
pf.seed(pf.parse_holdings("종목코드,평가금액,종목명\nAAPL,10000,Apple\n"),
        tranches=1, table=ST)
pf.resync("종목코드,평가금액,종목명\nAAPL,10000,Apple\n", cash=0.0, tranches=1,
          today=date(2026, 11, 21), table=ST, capital_table=RT)
after = store.read(RT).sort_values("기준일").iloc[-1]
check(abs(float(after["약정액"]) - 30_162.0) < 1,
      f"저장된 약정액은 **목표액** 그대로 (${float(after['약정액']):,.0f})")
check(pd.notna(after["램프시작일"]),
      f"램프시작일도 살아 있음 ({after['램프시작일']})")
cap_after = pf.load_capital(RT)
# load_capital은 '오늘' 기준으로 램프를 적용합니다. 재동기화를 11월 날짜로
# 돌렸어도 오늘이 10월이면 0개월차 값이 나오는 게 맞습니다 — 중요한 건
# 저장된 목표액과 시작일이 살아남아 다음 달에 올라갈 수 있다는 것입니다.
_m1 = pf._ramp(pd.Series({"램프시작일": after["램프시작일"],
                          "램프시작액": after["램프시작액"]}),
               float(after["약정액"]),
               today=pd.Timestamp(after["램프시작일"])
               + pd.DateOffset(months=1))["적용약정"]
check(abs(_m1 - 23_142) < 2,
      f"재동기화 뒤에도 다음 달이면 1개월차로 올라감 (${_m1:,.0f})")
check(float(cap_after.get("최종약정액") or 0) == 30_162.0,
      f"최종약정액으로 목표를 돌려줌 (${float(cap_after.get('최종약정액') or 0):,.0f})")

print("\n[자본 기록 없이 보유만 있을 때 — 2026-10-22 실측 상태]")
# seed만 하고 재동기화를 안 돌린 계좌입니다(미국 두 계좌가 정확히 이랬습니다:
# 보유 58·22종목, 자본 기록 0점). 그냥 두면 set_capital이 '현금 total로
# 시작'으로 보고 평가액을 total로 적어, 램프 시작액이 0이 되어 첫 달부터
# 전액이 배정됩니다 — 단계 인상이 무의미해집니다.
ST2, CT2 = "us_portfolio", "us_capital"
store.write(CT2, pd.DataFrame(columns=list(store.SCHEMAS[CT2])))
pf.reset_state(ST2)
pf.seed(pf.parse_holdings(
    "종목코드,평가금액,종목명\nAAPL,12000,Apple\nMSFT,7631.65,MS\n"),
    tranches=1, table=ST2)
rr = pf.set_capital(30_162.0, table=CT2, today=date(2026, 10, 22), ramp=True,
                    state_table=ST2)
check(abs(float(rr["평가액"]) - 19_631.65) < 1,
      f"보유 평가액을 실측으로 씀 (${float(rr['평가액']):,.2f}) — total로 덮지 않음")
check(abs(float(rr["약정액"]) - 19_631.65) < 1,
      f"0개월차 적용액 = 지금 보유 (${float(rr['약정액']):,.2f})")
check(abs(float(rr.get("목표액", 0)) - 30_162.0) < 1, "목표액은 30,162")
_m3 = pf._ramp(pd.Series({"램프시작일": pd.Timestamp("2026-10-22"),
                          "램프시작액": 19_631.65}), 30_162.0,
               today=date(2027, 1, 22))["적용약정"]
check(abs(_m3 - 30_162.0) < 1, f"3개월 뒤 목표 도달 (${_m3:,.0f})")

check(abs(float(pf.set_capital(
    9_000.0, table="us_capital_alt", today=date(2026, 10, 22),
    state_table="us_portfolio_alt")["평가액"]) - 9_000.0) < 1,
      "보유가 없으면 예전대로 현금 시작")

print("\n[램프를 안 쓰면 예전 그대로]")
NT = "us_capital_alt"
store.write(NT, pd.DataFrame(columns=list(store.SCHEMAS[NT])))
rn = pf.set_capital(6_064.0, table=NT, today=date(2026, 10, 21))
check(abs(float(rn["약정액"]) - 6_064.0) < 1,
      f"ramp 없이 설정하면 즉시 전액 (${float(rn['약정액']):,.0f})")
check("목표액" not in rn, "목표액 항목이 안 붙음")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 미국 주문서를 만들지 마세요.")
    sys.exit(1)
print("전체 통과 — 티커·테이블·램프가 모두 제자리입니다.")
