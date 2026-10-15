# -*- coding: utf-8 -*-
"""미국 월간 주문서 러너 한 바퀴 — 네트워크 없이, 가짜 데이터로.

여기서 막으려는 사고
  1. 약정액이 없는데 한국 기본값(QUANT_CAPITAL_KRW, 원화)으로 주문서가
     나오는 것. 4,000만원이 달러 계좌에 들어가면 종목당 배정액이 40만
     달러가 되어 전 종목을 사라는 주문서가 나옵니다.
  2. 두 계좌(상수/배우자)가 같은 테이블을 쓰는 것 — 한쪽 매도가 다른 쪽
     주문서에 나타납니다.
  3. 교차분할이 실제로 종목을 갈라놓는지. 설정만 있고 안 걸리면 두 계좌가
     같은 종목을 같은 날 사서 서로 호가를 밀어올립니다.
  4. 주문 수량이 정수인지 (소수점 매매는 쓰지 않기로 했습니다).
"""
import os
import sys
import tempfile
from datetime import date

_DD = tempfile.mkdtemp()
os.environ["QUANT_DATA_DIR"] = _DD
os.environ["QUANT_OUTPUT_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
import portfolio as pf  # noqa: E402
import store  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


# ── 가짜 미국 데이터 ─────────────────────────────────────────
rng = np.random.default_rng(7)
N = 400
SYMS = [f"T{i:03d}" for i in range(N)]
SECTORS = ["Tech", "Health", "Energy", "Finance", "Industrial"]
DAYS = pd.bdate_range("2025-10-01", "2026-10-15")

px = pd.DataFrame({
    "Date": np.repeat(DAYS, N),
    "Symbol": SYMS * len(DAYS),
})
base = pd.Series(rng.uniform(8, 300, N), index=SYMS)
drift = rng.normal(0, 0.012, (len(DAYS), N)).cumsum(axis=0)
px["Close"] = (base.values * np.exp(drift)).reshape(-1)
px["Volume"] = rng.uniform(2e5, 5e6, len(px))
px["Open"] = px["Close"]; px["High"] = px["Close"]; px["Low"] = px["Close"]
store.write("global_price", px)

tk = pd.DataFrame({
    "Symbol": SYMS, "Name": [f"{s} Corp" for s in SYMS],
    "Sector": [SECTORS[i % len(SECTORS)] for i in range(N)],
    "Exchange": "NASDAQ", "country": "US",
    "Market Cap": rng.uniform(5e8, 5e11, N),
    "date": pd.Timestamp("2026-10-15"),
})
store.write("global_ticker", tk)

QS = pd.to_datetime(["2024-12-31", "2025-03-31", "2025-06-30",
                     "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
ACC = ["NetIncome", "GrossProfit",
       "CashFlowFromContinuingOperatingActivities",
       "TotalAssets", "StockholdersEquity"]
rows = []
for s in SYMS:
    scale = rng.uniform(1e8, 1e10)
    for a in ACC:
        mult = {"NetIncome": 0.05, "GrossProfit": 0.25,
                "CashFlowFromContinuingOperatingActivities": 0.08,
                "TotalAssets": 1.0, "StockholdersEquity": 0.4}[a]
        for q in QS:
            rows.append({"Symbol": s, "date": q, "account": a,
                         "value": scale * mult * rng.uniform(0.8, 1.2),
                         "freq": "q", "filed": q + pd.Timedelta(days=40)})
store.write("global_fs", pd.DataFrame(rows))

last_px = px[px["Date"] == DAYS[-1]].set_index("Symbol")["Close"]
val = []
for s in SYMS:
    for m, v in (("PER", rng.uniform(5, 40)), ("PBR", rng.uniform(0.5, 8)),
                 ("PCR", rng.uniform(3, 30)), ("PSR", rng.uniform(0.3, 10)),
                 ("DY", rng.uniform(0, 0.05))):
        val.append({"Symbol": s, "date": pd.Timestamp("2026-10-15"),
                    "지표": m, "값": v})
store.write("global_value", pd.DataFrame(val))

print(f"가짜 데이터: 종목 {N} · 주가 {len(px):,}행 · 재무 {len(rows):,}행")

import run_us_monthly_orders as uo  # noqa: E402

# ── 1. 약정액 없이 돌리면 멈추는가 ───────────────────────────
print("\n[약정액이 없으면 멈춘다 — 원화가 달러 계좌에 새는 것을 막음]")
try:
    uo.main(tranches=4, profile="main")
    check(False, "약정액 없이 주문서가 나왔습니다 — 막아야 합니다")
except RuntimeError as e:
    check("약정액" in str(e), f"멈췄고 무엇을 해야 하는지 알려줌: {str(e)[:50]}…")
except Exception as e:                                     # noqa: BLE001
    check(False, f"엉뚱한 예외: {type(e).__name__} {e}")

# ── 2. 약정액 설정 ───────────────────────────────────────────
print("\n[약정액 설정 (USD)]")
for key, amt in (("main", 28_000.0), ("alt", 14_000.0)):
    p = uo._prof(key)
    r = pf.set_capital(amt, table=p["capital_table"])
    check(abs(float(r["약정액"]) - amt) < 1e-6,
          f"{p['라벨']} 약정액 ${r['약정액']:,.0f} · 종목당 "
          f"${float(r['종목당배정액']):,.2f}")
check(store.read("us_capital")is not None, "us_capital 테이블에 들어감")
check(pf.load_capital("kor_capital").get("약정액") in (None, 0, 0.0),
      "한국 테이블은 건드리지 않음")

# ── 3. 두 계좌 주문서 ────────────────────────────────────────
print("\n[주문서 — 두 계좌]")
got = {}
for key in ("main", "alt"):
    rc = uo.main(tranches=4, profile=key)
    check(rc == 0, f"{key} 주문서 생성 (반환 {rc})")
    p = uo._prof(key)
    got[key] = pf.load_state(p["state_table"])

for key in ("main", "alt"):
    st = got[key]
    check(len(st) > 0, f"{key} 보유 상태 {len(st)}종목")
    bad = [c for c in st["종목코드"].astype(str) if not c.startswith("T")]
    check(not bad, f"{key} 종목코드가 전부 티커 모양 (이상 {bad[:3]})")

print("\n[두 계좌가 섞이지 않는가]")
check(set(got["main"]["종목코드"]) != set(got["alt"]["종목코드"]),
      "두 계좌의 보유가 다름")
_ov = len(set(got["main"]["종목코드"]) & set(got["alt"]["종목코드"]))
_n = min(len(got["main"]), len(got["alt"]))
check(_ov < _n, f"겹치는 종목 {_ov}/{_n} — 교차분할이 실제로 갈라놓음 "
                f"(공용 {config.PROFILES['main']['split']['shared']:.0%} 설정)")

print("\n[원장]")
log = store.read("us_trades")
check(len(log) > 0, f"us_trades에 {len(log)}건")
check(set(log["계좌"].astype(str)) == {"main", "alt"},
      f"두 계좌 모두 기록 ({sorted(set(log['계좌'].astype(str)))})")
qty = pd.to_numeric(log["수량"], errors="coerce").dropna()
check(bool((qty == qty.round()).all()),
      "주문 수량이 전부 정수 (소수점 매매 안 함)")
check(store.read("kor_trades").empty, "한국 원장은 비어 있음")

print("\n[1주 제약이 보고되는가]")
import json  # noqa: E402
j = json.loads((config.OUTPUT_DIR / "us_orders_latest.json")
               .read_text(encoding="utf-8"))
check(j["통화"] == "USD", f"주문서 json에 통화 표기 ({j['통화']})")
check("가격초과제외" in j, f"가격초과제외 보고 ({j.get('가격초과제외')}종목)")
check(j["체결전_실제보유"] != j["주문후_보유예정"] or j["매수"] == 0,
      f"체결 전/후 보유를 따로 적음 ({j['체결전_실제보유']} → "
      f"{j['주문후_보유예정']})")

print("\n[모델 산출물]")
p = uo._prof("main")
check((config.OUTPUT_DIR / p["model_file"]).exists(),
      f"{p['model_file']} 생성")
check((config.OUTPUT_DIR / p["hold_file"]).exists(),
      f"{p['hold_file']} 생성 (순위 버퍼)")
wide = pd.read_parquet(config.OUTPUT_DIR / p["model_file"])
check(len(wide) > config.N_PORTFOLIO,
      f"주문 엔진용 명단이 상위 100보다 깊음 ({len(wide)}종목)")
check("종가" in wide.columns and wide["종가"].notna().any(),
      "종가가 들어 있음 (수량 계산에 필요)")
hold = pd.read_parquet(config.OUTPUT_DIR / p["hold_file"])
check(len(hold) >= len(wide.head(config.N_PORTFOLIO)),
      f"유지 명단 {len(hold)}종목 ≥ 보유 목표 {config.N_PORTFOLIO}")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 실제 주문을 내지 마세요.")
    sys.exit(1)
print("전체 통과 — 약정액 없이는 안 돌고, 두 계좌가 섞이지 않습니다.")
