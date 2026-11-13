# -*- coding: utf-8 -*-
"""대시보드 성과 계산 — 두 시장을 섞지 않는가.

여기서 막으려는 사고
  1. 미국 티커가 6자리 0채움에 망가지는 것. 'AAPL' → '00AAPL',
     'KO' → '0000KO'가 되면 주가와 종목코드가 안 맞아 평가액 곡선과
     종목별 손익이 **조용히 비어 버립니다** (화면에 오류가 아니라 '–'만
     뜨므로 알아채기 어렵습니다). store._normalize에서 이미 한 번 낸
     사고입니다(2026-10-21).
  2. 달러 평가액이 원화 곡선에 섞이는 것. 한 점이라도 들어가면 수익률이
     통째로 거짓이 되고 되돌릴 방법이 없습니다.
  3. 한국 경로의 회귀 — '70'이 '000070'으로 복원되지 않으면 한국 성과가
     전부 '원가미상'으로 떨어집니다.

Streamlit이 깔려 있으면 app.py를 실제로 한 번 렌더해 두 성과 탭이 모두
예외 없이 그려지는지도 봅니다. 없으면 그 부분만 건너뜁니다 (파이프라인
requirements.txt에는 streamlit이 없습니다 — 대시보드 전용입니다).
"""
import os
import sys
import tempfile

os.environ["QUANT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import perf  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


# ── 1. 종목코드 정규화 ───────────────────────────────────────
print("[종목코드 정규화 — 숫자만 0을 채운다]")
cases = [("70", "000070"), ("005930", "005930"), ("5930", "005930"),
         ("AAPL", "AAPL"), ("KO", "KO"), ("M", "M"), ("BRK.B", "BRK.B"),
         ("BF-B", "BF-B"), ("WHLRP", "WHLRP"), (" NUS ", "NUS")]
got = perf._code6(pd.Series([c for c, _ in cases])).tolist()
for (src, want), g in zip(cases, got):
    check(g == want, f"{src!r:10s} → {g!r} (기대 {want!r})")
check(all(perf._c6(c) == w for c, w in cases),
      "스칼라 경로도 같은 결과")

# ── 2. 미국 계좌 한 바퀴 ─────────────────────────────────────
print("\n[미국 계좌 — 티커가 살아남고 금액이 달러로 남는가]")
TICK = ["AAPL", "KO", "M", "BRK.B", "WHLRP"]
DAYS = pd.bdate_range("2026-10-01", "2026-11-04")
rng = np.random.default_rng(3)
base = dict(zip(TICK, [230.0, 62.0, 24.0, 480.0, 11.6]))
px_rows = []
for i, d in enumerate(DAYS):
    for t in TICK:
        px_rows.append({"날짜": d, "종목코드": t,
                        "종가": base[t] * (1 + 0.0004 * i
                                         + rng.normal(0, 0.004))})
us_price = pd.DataFrame(px_rows)

SNAPS = [pd.Timestamp("2026-10-05"), pd.Timestamp("2026-10-20"),
         pd.Timestamp("2026-11-04")]
hlog_rows, trade_rows = [], []
for s in SNAPS:
    for t in TICK:
        q = {"AAPL": 2, "KO": 8, "M": 20, "BRK.B": 1, "WHLRP": 9}[t]
        p = base[t]
        hlog_rows.append({"기준일": s, "계좌": "main", "종목코드": t,
                          "종목명": f"{t} Inc", "평가금액": q * p, "수량": q})
for t in TICK:
    trade_rows.append({"기준일": SNAPS[0], "계좌": "main", "종목코드": t,
                       "구분": "매수", "수량": 1, "주가": base[t],
                       "체결수량": {"AAPL": 2, "KO": 8, "M": 20,
                                 "BRK.B": 1, "WHLRP": 9}[t],
                       "체결가": base[t], "체결일": SNAPS[0]})
us_hlog = pd.DataFrame(hlog_rows)
us_trades = pd.DataFrame(trade_rows)

codes = set(perf._code6(us_hlog["종목코드"]))
check(codes == set(TICK), f"보유 기록 코드가 티커 그대로 — {sorted(codes)}")
check(not any(str(c).startswith("00") for c in codes),
      "'00AAPL' 같은 코드가 없음")

upx = perf.price_pivot(us_price, codes)
check(list(upx.columns) == sorted(TICK),
      f"주가 피벗 칸이 티커 — {list(upx.columns)}")
check(not upx.empty and upx.notna().any().all(),
      "모든 티커에 주가가 붙음 (하나라도 비면 곡선이 비어 버립니다)")

upos = perf.positions_timeline(us_hlog, "main", upx)
check(len(upos) == len(SNAPS), f"스냅샷 {len(upos)}개 ({len(SNAPS)} 기대)")
check(set(upos.columns) == set(TICK), "보유 표 칸이 티커 그대로")

uval = perf.daily_value(upos, upx)
check(not uval.empty, f"평가액 곡선 {len(uval)}일")
check(float(uval.max()) < 1e5,
      f"평가액이 달러 자릿수 (최대 ${float(uval.max()):,.2f}) — "
      f"원화 자릿수면 통화가 섞인 것입니다")

uflows = perf.align_flows(perf.net_flows(us_trades, "main"),
                          uval.index, upos.index)
utw = perf.twr(uval, uflows)
check(len(utw) >= 2, f"TWR {len(utw)}점")
check(abs(float(utw['누적'].iloc[-1]) - 1) < 0.5,
      f"수익률이 현실적인 범위 ({(float(utw['누적'].iloc[-1]) - 1) * 100:+.2f}%)")

upnl = perf.position_pnl(us_trades, us_hlog, upx, "main")
check(len(upnl) == len(TICK), f"종목별 손익 {len(upnl)}행 ({len(TICK)} 기대)")
check(set(upnl["종목코드"]) == set(TICK),
      f"손익 표 코드도 티커 — {sorted(upnl['종목코드'])}")
check(int((upnl["원가출처"] == "원가미상").sum()) == 0,
      "원가미상이 없음 (티커가 망가지면 전부 원가미상이 됩니다)")
check(float(pd.to_numeric(upnl["평가금액"]).sum()) < 1e5,
      f"손익 표 금액도 달러 (${float(pd.to_numeric(upnl['평가금액']).sum()):,.2f})")

# ── 3. 한국 경로 회귀 ────────────────────────────────────────
print("\n[한국 경로 회귀 — '70'이 '000070'으로 복원되는가]")
KOR = ["005930", "000660", "000070"]
kp_rows = []
for i, d in enumerate(DAYS):
    for c, p in zip(KOR, (72_000.0, 198_000.0, 15_400.0)):
        kp_rows.append({"날짜": d, "종목코드": c, "종가": p * (1 + 0.0003 * i)})
kor_price = pd.DataFrame(kp_rows)
kor_hlog = pd.DataFrame([
    {"기준일": s, "계좌": "main", "종목코드": c.lstrip("0"),   # 0이 떨어진 꼴
     "종목명": c, "평가금액": q * p, "수량": q}
    for s in SNAPS
    for c, q, p in zip(KOR, (15, 3, 40), (72_000.0, 198_000.0, 15_400.0))])
kor_trades = pd.DataFrame([
    {"기준일": SNAPS[0], "계좌": "main", "종목코드": c, "구분": "매수",
     "수량": q, "주가": p, "체결수량": q, "체결가": p, "체결일": SNAPS[0]}
    for c, q, p in zip(KOR, (15, 3, 40), (72_000.0, 198_000.0, 15_400.0))])

kcodes = set(perf._code6(kor_hlog["종목코드"]))
check(kcodes == set(KOR), f"0이 떨어진 코드가 복원됨 — {sorted(kcodes)}")
kpx = perf.price_pivot(kor_price, kcodes)
kpos = perf.positions_timeline(kor_hlog, "main", kpx)
check(set(kpos.columns) == set(KOR), "한국 보유 표가 6자리 코드")
kval = perf.daily_value(kpos, kpx)
check(float(kval.min()) > 1e6,
      f"한국 평가액은 원화 자릿수 ({float(kval.min()):,.0f}원)")
kpnl = perf.position_pnl(kor_trades, kor_hlog, kpx, "main")
check(int((kpnl["원가출처"] == "원가미상").sum()) == 0,
      "한국도 원가미상 없음")

print("\n[두 시장이 섞이지 않았는가 — 가장 중요]")
check(float(uval.max()) < 1e5 and float(kval.min()) > 1e6,
      f"달러 ${float(uval.max()):,.2f} / 원화 {float(kval.min()):,.0f}원 — "
      f"자릿수가 섞이지 않음")
check(set(upnl["종목코드"]).isdisjoint(set(kpnl["종목코드"])),
      "두 시장의 종목이 겹치지 않음")

# ── 3.5 팔린 종목이 평가액에 남지 않는가 (2026-11-11 실측) ──
# 재동기화 스냅샷은 그 날 보유의 **전체**입니다. 어떤 종목이 스냅샷에 없다는
# 것은 '모른다'가 아니라 '팔아서 없다'는 뜻입니다. 예전에는 비워 뒀고,
# daily_value의 ffill이 직전 수량을 끌고 와서 팔린 종목이 평가액에 계속
# 남았습니다. 미국 main이 실제 $12,165인데 $22,832로 계산돼 7일 수익률이
# +53%로 찍혔습니다(실제 -0.8%).
print("\n[팔린 종목이 평가액에 남지 않는가 — +53%의 원인]")
D0, D1 = pd.Timestamp("2026-10-31"), pd.Timestamp("2026-11-04")
PRICE = {"AAA": 100.0, "BBB": 50.0, "CCC": 20.0, "DDD": 10.0, "EEE": 25.0}
BEFORE = {"AAA": 10, "BBB": 10, "CCC": 10}          # $1,000+$500+$200 = 1,700
AFTER = {"AAA": 10, "DDD": 20, "EEE": 8}            # $1,000+$200+$200 = 1,400
rows = []
for d, hold in ((D0, BEFORE), (D1, AFTER)):
    for c, q in hold.items():
        rows.append({"기준일": d, "계좌": "main", "종목코드": c,
                     "종목명": c, "평가금액": q * PRICE[c], "수량": q})
hl = pd.DataFrame(rows)
DD = pd.bdate_range("2026-10-29", "2026-11-10")
pr = pd.DataFrame([{"날짜": d, "종목코드": c, "종가": p}
                   for d in DD for c, p in PRICE.items()])
spx = perf.price_pivot(pr, set(PRICE))
spos = perf.positions_timeline(hl, "main", spx)
check(float(spos.loc[D1, "BBB"]) == 0.0 and float(spos.loc[D1, "CCC"]) == 0.0,
      f"스냅샷에서 빠진 종목은 0주 (BBB {spos.loc[D1, 'BBB']:.0f} · "
      f"CCC {spos.loc[D1, 'CCC']:.0f})")
sval = perf.daily_value(spos, spx)
check(abs(float(sval.loc[D0_ := DD[DD >= D0][0]]) - 1700.0) < 1e-6,
      f"첫 스냅샷 평가액 ${float(sval.loc[D0_]):,.2f} (기대 $1,700)")
check(abs(float(sval.loc[D1]) - 1400.0) < 1e-6,
      f"둘째 스냅샷 평가액 ${float(sval.loc[D1]):,.2f} (기대 $1,400 — "
      f"예전에는 팔린 BBB·CCC $700이 남아 $2,100이었습니다)")

# 판 돈으로 산 리밸런싱 — 수익률은 0에 가까워야 합니다
tr_rows = [{"기준일": D0, "계좌": "main", "구분": "매도", "종목코드": c,
            "체결가": PRICE[c], "체결수량": BEFORE[c], "체결일": D1}
           for c in ("BBB", "CCC")]
tr_rows += [{"기준일": D0, "계좌": "main", "구분": "매수", "종목코드": c,
             "체결가": PRICE[c], "체결수량": AFTER[c], "체결일": D1}
            for c in ("DDD", "EEE")]
st_tr = pd.DataFrame(tr_rows)
sfl = perf.align_flows(perf.net_flows(st_tr, "main"), sval.index, spos.index)
stw = perf.twr(sval, sfl)
check(abs(float(sfl.get(D1, 0.0)) - (-300.0)) < 1e-6,
      f"순현금흐름 ${float(sfl.get(D1, 0.0)):,.2f} (매수 400 - 매도 700)")
check(abs(float(stw['누적'].iloc[-1]) - 1.0) < 1e-9,
      f"판 돈으로 사면 수익률 0 ({(float(stw['누적'].iloc[-1]) - 1) * 100:+.2f}%)")
strust = perf.twr_trust(stw)
check(strust["신뢰"], f"정상 리밸런싱은 믿을 수 있다고 판정 ({strust['사유'][:40]})")

print("\n[흐름과 평가액이 어긋나면 막는가 — 예전 판정이 통과시킨 모양]")
# 평가액은 +3,236인데 흐름은 -7,311 → 잔차 +10,547. 예전 판정은 '흐름이
# 크니 설명됨'으로 통과시켰습니다. 실제 11/04 숫자를 그대로 씁니다.
bad = pd.DataFrame({"평가액": [19631.65, 22867.25], "순입출금": [0.0, -7311.5]},
                   index=pd.to_datetime(["2026-11-02", "2026-11-04"]))
bad["일수익률"] = [0.0, 0.5372]
bad["누적"] = [1.0, 1.5372]
bt = perf.twr_trust(bad)
check(not bt["신뢰"], "막음 (예전 판정은 흐름이 커서 통과시켰습니다)")
check("설명되지 않는" in bt["사유"], f"사유를 남김: {bt['사유'][:50]}…")
brief = perf.trust_brief(bt)
check("11/04" in brief and len(brief) < 60,
      f"요약 표용 짧은 사유에 날짜가 남음: {brief!r}")
check(perf.trust_brief({"신뢰": True}) == "", "믿을 수 있으면 짧은 사유는 빈칸")

# 반대로 흐름이 평가액 변화를 제대로 설명하면 통과해야 합니다 (오탐 방지)
ok_t = pd.DataFrame({"평가액": [19631.65, 12165.47], "순입출금": [0.0, -7311.5]},
                    index=pd.to_datetime(["2026-11-02", "2026-11-04"]))
ok_t["일수익률"] = [0.0, -0.0079]
ok_t["누적"] = [1.0, 0.9921]
check(perf.twr_trust(ok_t)["신뢰"],
      "같은 날 실제 평가액($12,165)이면 통과 — 오탐이 아님")

# ── 3.7 원장 기준 손익 — 예수금 없이, 팔린 종목까지 ─────────
# 예수금을 기록하지 않기로 했으므로(config.TRACK_CASH) 손익은 원장의
# 매입가·매도가로만 셉니다. 팔아서 끝난 종목의 실현손익이 여기서 나옵니다.
print("\n[원장 기준 손익 — 실현 + 미실현]")
LDAYS = pd.bdate_range("2026-11-02", "2026-11-10")
LPX = pd.DataFrame([{"날짜": d, "종목코드": c, "종가": p}
                    for d in LDAYS
                    for c, p in (("WIN", 120.0), ("LOSE", 8.0),
                                 ("HOLD", 55.0), ("OLD", 30.0))])
lpx = perf.price_pivot(LPX, {"WIN", "LOSE", "HOLD", "OLD"})
LT = pd.DataFrame([
    # 10주 @100 산 뒤 전량 @120 매도 → 실현 +200
    {"기준일": "2026-11-02", "계좌": "main", "구분": "매수", "종목코드": "WIN",
     "종목명": "WIN", "체결가": 100.0, "체결수량": 10, "체결일": "2026-11-02"},
    {"기준일": "2026-11-04", "계좌": "main", "구분": "매도", "종목코드": "WIN",
     "종목명": "WIN", "체결가": 120.0, "체결수량": 10, "체결일": "2026-11-04"},
    # 20주 @10 산 뒤 절반 @8 매도 → 실현 -20, 남은 10주 미실현 -20
    {"기준일": "2026-11-02", "계좌": "main", "구분": "매수", "종목코드": "LOSE",
     "종목명": "LOSE", "체결가": 10.0, "체결수량": 20, "체결일": "2026-11-02"},
    {"기준일": "2026-11-04", "계좌": "main", "구분": "비중축소",
     "종목코드": "LOSE", "종목명": "LOSE", "체결가": 8.0, "체결수량": 10,
     "체결일": "2026-11-04"},
    # 5주 @50 사서 보유 → 미실현 +25
    {"기준일": "2026-11-02", "계좌": "main", "구분": "매수", "종목코드": "HOLD",
     "종목명": "HOLD", "체결가": 50.0, "체결수량": 5, "체결일": "2026-11-02"},
    # 원장에 매입이 없는 종목을 매도 → 원가미상, 손익에서 제외
    {"기준일": "2026-11-02", "계좌": "main", "구분": "매도", "종목코드": "OLD",
     "종목명": "OLD", "체결가": 30.0, "체결수량": 100, "체결일": "2026-11-02"},
])
for _c in ("기준일", "체결일"):
    LT[_c] = pd.to_datetime(LT[_c])
lp = perf.ledger_pnl(LT, "main", lpx)
check(abs(lp["실현손익"] - 180.0) < 1e-6,
      f"실현손익 ${lp['실현손익']:,.2f} (WIN +200, LOSE -20 → 기대 $180)")
check(abs(lp["미실현손익"] - 5.0) < 1e-6,
      f"미실현손익 ${lp['미실현손익']:,.2f} (LOSE -20, HOLD +25 → 기대 $5)")
check(abs(lp["손익"] - 185.0) < 1e-6, f"합계 ${lp['손익']:,.2f} (기대 $185)")
check(lp["원가미상_매도종목"] == 1 and abs(lp["원가미상_매도금액"] - 3000.0) < 1e-6,
      f"원가미상 매도 {lp['원가미상_매도종목']}종목 "
      f"${lp['원가미상_매도금액']:,.2f} (기대 1종목 $3,000)")
_d = lp["종목별"].set_index("종목코드")
check(pd.isna(_d.loc["OLD", "손익"]),
      "원가미상 종목의 손익은 0이 아니라 빈 값 — 추정해 채우지 않음")
check(_d.loc["WIN", "상태"] == "정리" and _d.loc["HOLD", "상태"] == "보유",
      f"상태 표기 (WIN {_d.loc['WIN', '상태']} · HOLD {_d.loc['HOLD', '상태']})")
check(abs(float(_d.loc["LOSE", "평단"]) - 10.0) < 1e-9,
      f"일부 매도 뒤 평단 유지 ${float(_d.loc['LOSE', '평단']):,.2f} (기대 $10)")
check(abs(float(_d.loc["LOSE", "보유수량"]) - 10.0) < 1e-9,
      f"일부 매도 뒤 보유수량 {float(_d.loc['LOSE', '보유수량']):,.0f}주")
check(perf.ledger_pnl(pd.DataFrame(), "main", lpx)["손익"] == 0.0,
      "원장이 비어 있으면 0")

print("\n[같은 날 매도를 먼저 본다 — 평단 희석 방지]")
SD = pd.DataFrame([
    {"기준일": "2026-11-02", "계좌": "main", "구분": "매수", "종목코드": "HOLD",
     "종목명": "HOLD", "체결가": 50.0, "체결수량": 10, "체결일": "2026-11-02"},
    # 같은 날: 10주를 @60에 팔고, 다시 10주를 @100에 삼
    {"기준일": "2026-11-04", "계좌": "main", "구분": "매수", "종목코드": "HOLD",
     "종목명": "HOLD", "체결가": 100.0, "체결수량": 10, "체결일": "2026-11-04"},
    {"기준일": "2026-11-04", "계좌": "main", "구분": "매도", "종목코드": "HOLD",
     "종목명": "HOLD", "체결가": 60.0, "체결수량": 10, "체결일": "2026-11-04"},
])
for _c in ("기준일", "체결일"):
    SD[_c] = pd.to_datetime(SD[_c])
sd = perf.ledger_pnl(SD, "main", lpx)
check(abs(sd["실현손익"] - 100.0) < 1e-6,
      f"실현손익 ${sd['실현손익']:,.2f} — @50 10주를 @60에 판 것 (기대 $100). "
      f"매수를 먼저 보면 평단이 75로 희석돼 -$150이 됩니다")

# ── 4. app.py 실제 렌더 (streamlit이 있으면) ─────────────────
print("\n[app.py 렌더 — 두 성과 탭이 예외 없이 그려지는가]")
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:                                         # noqa: BLE001
    print(f"[건너뜀] streamlit이 없어 렌더 점검은 생략합니다 ({type(e).__name__}) "
          f"— 대시보드 전용 의존성입니다(requirements-app.txt)")
else:
    _app = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app.py")
    at = AppTest.from_file(_app, default_timeout=300)
    for k, v in (("GH_TOKEN", "fake"), ("GH_REPO", "nobody/nothing"),
                 ("STATE_REPO", "nobody/state"), ("STATE_TOKEN", "fake")):
        at.secrets[k] = v
    at.run()
    check(not at.exception,
          f"예외 없음 ({[str(e.value)[:60] for e in at.exception]})")
    labels = [t.label for t in at.tabs]
    check(any("성과(한국)" in x for x in labels), f"성과(한국) 탭 ({labels[:2]})")
    check(any("성과(미국)" in x for x in labels), f"성과(미국) 탭 ({labels[:2]})")
    infos = " ".join(i.value for i in at.info)
    check("한국 계좌" in infos and "미국 계좌" in infos,
          "두 시장 모두 안내문을 냄 (한쪽만 렌더되는 사고 방지)")

    # ── 4b. 신뢰 점검이 숨긴 계좌가 요약 표에 '측정 전'으로 숨지 않는가 ──
    # 2026-11-13 실측: 배우자 계좌 10/18 보유목록에 KT가 2주인데 체결내역은
    # 1주뿐이라, 10/19 평가액 점프가 설명되지 않아 TWR이 숨겨졌습니다.
    # 맨 위 노란 상자에는 사유가 떴는데 성과 탭 요약 표는 '측정 전'이었습니다.
    # 같은 모양의 데이터를 가짜 GitHub 응답으로 흘려 넣고 렌더해 봅니다.
    print("\n[요약 표 — 입출금누락 계좌의 상태와 사유가 보이는가]")
    import io as _io
    import json as _json
    import requests as _rq
    import streamlit as _st

    def _pq(df: pd.DataFrame) -> bytes:
        buf = _io.BytesIO()
        df.to_parquet(buf, index=False)
        return buf.getvalue()

    PD = pd.bdate_range("2026-10-05", "2026-10-30")
    CODES = {"000001": 10000.0, "000002": 10000.0, "030200": 50000.0}
    _r = np.random.default_rng(7)
    _px = [{"날짜": d, "종목코드": c,
            "종가": round(p * (1 + 0.003 * _r.standard_normal()), 0)}
           for d in PD for c, p in CODES.items()]
    _price = pd.DataFrame(_px)

    def _t(d):
        return pd.Timestamp(d)

    _trades = pd.DataFrame([
        # 상수: 깨끗한 계좌 — 한 번 사고 그대로
        {"기준일": _t("2026-10-07"), "계좌": "main", "구분": "매수",
         "종목코드": "000001", "종목명": "에이", "체결가": 10000.0,
         "체결수량": 100.0, "체결일": _t("2026-10-07")},
        # 배우자: 10/07에 둘, 10/16에 KT 1주
        {"기준일": _t("2026-10-07"), "계좌": "alt", "구분": "매수",
         "종목코드": "000001", "종목명": "에이", "체결가": 10000.0,
         "체결수량": 20.0, "체결일": _t("2026-10-07")},
        {"기준일": _t("2026-10-07"), "계좌": "alt", "구분": "매수",
         "종목코드": "000002", "종목명": "비", "체결가": 10000.0,
         "체결수량": 10.0, "체결일": _t("2026-10-07")},
        {"기준일": _t("2026-10-16"), "계좌": "alt", "구분": "매수",
         "종목코드": "030200", "종목명": "KT", "체결가": 50000.0,
         "체결수량": 1.0, "체결일": _t("2026-10-16")},
    ])
    _hl = []
    for snap in ("2026-10-10", "2026-10-18"):
        _hl.append({"기준일": _t(snap), "계좌": "main", "종목코드": "000001",
                    "종목명": "에이", "평가금액": 1_000_000.0, "수량": 100.0})
        _hl.append({"기준일": _t(snap), "계좌": "alt", "종목코드": "000001",
                    "종목명": "에이", "평가금액": 200_000.0, "수량": 20.0})
        _hl.append({"기준일": _t(snap), "계좌": "alt", "종목코드": "000002",
                    "종목명": "비", "평가금액": 100_000.0, "수량": 10.0})
    # 체결은 1주인데 보유목록은 2주 — 1주(약 5만원)가 수익으로 잡힙니다
    _hl.append({"기준일": _t("2026-10-18"), "계좌": "alt", "종목코드": "030200",
                "종목명": "KT", "평가금액": 100_000.0, "수량": 2.0})
    STATE = {"kor_trades.parquet": _pq(_trades),
             "kor_holdings_log.parquet": _pq(pd.DataFrame(_hl))}
    _last = PD.max()
    ASSETS = {
        "kor_price.parquet": _pq(_price),
        "kor_ticker.parquet": _pq(pd.DataFrame(
            [{"종목코드": c, "시가총액": p * 1e6, "종가": p, "기준일": _last}
             for c, p in CODES.items()])),
        "kor_sector.parquet": _pq(pd.DataFrame(
            [{"CMP_CD": c, "SEC_NM_KOR": "산업재", "기준일": _last}
             for c in CODES])),
    }

    class _Resp:
        def __init__(self, code=404, content=b"", payload=None):
            self.status_code, self.content = code, content
            self._payload = payload
            self.ok = code < 400
            self.text = content.decode("latin-1") if content else ""

        def json(self):
            return self._payload if self._payload is not None else {}

        def raise_for_status(self):
            if not self.ok:
                raise _rq.HTTPError(str(self.status_code))

    def _fake_get(url, *a, **k):
        url = str(url)
        if url.endswith("/repos/nobody/nothing/releases/tags/data-store"):
            return _Resp(200, b"{}", {"assets": [
                {"name": n, "url": f"fake://asset/{n}", "size": len(b),
                 "updated_at": "2026-10-30T12:00:00Z"} for n, b in ASSETS.items()]})
        if url.startswith("fake://asset/"):
            n = url.rsplit("/", 1)[-1]
            return _Resp(200, ASSETS[n]) if n in ASSETS else _Resp()
        if "/repos/nobody/state/contents/state/" in url:
            n = url.rsplit("/", 1)[-1]
            return _Resp(200, STATE[n]) if n in STATE else _Resp()
        return _Resp()

    _orig_get = _rq.get
    _rq.get = _fake_get
    try:
        _st.cache_data.clear()
        at2 = AppTest.from_file(_app, default_timeout=300)
        for k, v in (("GH_TOKEN", "fake"), ("GH_REPO", "nobody/nothing"),
                     ("STATE_REPO", "nobody/state"), ("STATE_TOKEN", "fake")):
            at2.secrets[k] = v
        at2.run()
    finally:
        _rq.get = _orig_get
        _st.cache_data.clear()
    check(not at2.exception,
          f"예외 없음 ({[str(e.value)[:80] for e in at2.exception]})")
    summ = next((d.value for d in at2.dataframe
                 if getattr(d.value, "index", None) is not None
                 and d.value.index.name == "항목"
                 and "상태" in d.value.index), None)
    check(summ is not None, "성과(한국) 요약 표가 그려짐")
    if summ is not None:
        st_alt = str(summ.loc["상태", "배우자"])
        st_main = str(summ.loc["상태", "상수"])
        check(st_alt == "체결내역 필요",
              f"배우자 상태 = {st_alt!r} ('측정 전'이면 사고 재발)")
        check(st_main == "측정 중", f"상수 상태 = {st_main!r} (깨끗한 계좌는 그대로)")
        why = str(summ.loc["숨긴 사유", "배우자"]) if "숨긴 사유" in summ.index else ""
        check("10/19" in why, f"숨긴 사유에 날짜가 보임 — {why!r}")
        check(str(summ.loc["수익률 (시간가중)", "배우자"]) == "숨김",
              "시간가중 칸은 '–'가 아니라 '숨김'")
    warns = " ".join(w.value for w in at2.warning)
    check("배우자" in warns and "alt·" not in warns,
          "경고문이 'alt'가 아니라 '배우자'로 적힘")
    check(warns.count("10/19") >= 2,
          "맨 위 상자와 성과 탭 경고 모두 날짜를 보여 줌")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 대시보드를 배포하지 마세요.")
    sys.exit(1)
print("전체 통과 — 티커가 살아 있고, 달러와 원화가 섞이지 않습니다.")

# DuckDB가 인터프리터 종료 중에 SIGABRT를 내는 일이 있습니다 (다른 점검
# 파일들과 같은 이유 — exit 134가 '점검 실패'로 읽힙니다). 결과를 확정한
# 뒤 바로 끝냅니다.
sys.stdout.flush()
sys.stderr.flush()
os._exit(1 if FAIL else 0)
