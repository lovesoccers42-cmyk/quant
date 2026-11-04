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
