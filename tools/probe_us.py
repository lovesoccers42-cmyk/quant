# -*- coding: utf-8 -*-
"""미국 데이터가 백테스트에 쓸 만큼 긴지 먼저 확인합니다.

    python tools/probe_us.py

왜 먼저 재보나: 미국 재무제표는 yfinance에서 받아오는데, yfinance의 분기
재무제표는 보통 최근 4~5분기뿐입니다. 연간도 4~5년 정도입니다. 그러면
한국처럼 9.5년 백테스트가 불가능합니다 — TTM에 4분기, 모멘텀에 1년,
공시지연 90일이 필요하니 재무제표 기간에서 1.5년 가까이 깎입니다.

돌려보고 막히는 걸 확인하는 것보다, 먼저 재서 '가능한 기간'을 알고 시작하는
게 낫습니다. 안 되면 미국 검증 자체를 다른 방법으로 바꿔야 합니다.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import config  # noqa: E402
import store  # noqa: E402

NEEDED_YEARS_NOTE = (
    "백테스트 유효기간 ≈ 재무제표 기간 − 1.5년 (TTM 4분기 + 모멘텀 1년 + 지연 90일)")


def span(df: pd.DataFrame, col: str) -> str:
    if df.empty or col not in df.columns:
        return "없음"
    d = pd.to_datetime(df[col], errors="coerce").dropna()
    if d.empty:
        return "날짜 없음"
    yrs = (d.max() - d.min()).days / 365.25
    return f"{d.min():%Y-%m-%d} ~ {d.max():%Y-%m-%d}  ({yrs:.1f}년)"


def main() -> int:
    print("=" * 66)
    print("  미국 데이터 점검")
    print("=" * 66)

    rows = []
    for table, datecol in (("global_ticker", "date"), ("global_price", "Date"),
                           ("global_fs", "date"), ("global_value", "date")):
        df = store.read(table)
        rows.append({"테이블": table, "행수": len(df), "기간": span(df, datecol)})
    print(pd.DataFrame(rows).to_string(index=False))

    fs = store.read("global_fs")
    if fs.empty:
        print("\n  global_fs가 비어 있습니다 — 미국 월간 실행을 먼저 돌리세요"
              " (run_us_monthly.py).")
        return 2

    fs["date"] = pd.to_datetime(fs["date"], errors="coerce")
    print("\n  재무제표 빈도별:")
    for f, g in fs.groupby("freq"):
        d = g["date"].dropna()
        n_sym = g["Symbol"].nunique()
        print(f"    freq={f}: {len(g):>9,}행 · {n_sym:>5}종목 · "
              f"{d.min():%Y-%m} ~ {d.max():%Y-%m} "
              f"({(d.max() - d.min()).days / 365.25:.1f}년)")

    # 종목당 분기 수 — TTM(4분기)이 나오는 종목이 얼마나 되는지
    q = fs[fs["freq"] == "q"]
    if not q.empty:
        per = q.groupby("Symbol")["date"].nunique()
        print(f"\n  종목당 분기 수: 중앙 {per.median():.0f} · "
              f"4분기 이상 {int((per >= 4).sum())}/{len(per)}종목")

    price = store.read("global_price")
    pyrs = 0.0
    if not price.empty:
        pd_ = pd.to_datetime(price["Date"], errors="coerce").dropna()
        pyrs = (pd_.max() - pd_.min()).days / 365.25

    fyrs = (fs["date"].max() - fs["date"].min()).days / 365.25
    usable = max(0.0, min(pyrs, fyrs) - 1.5)
    print(f"\n  {NEEDED_YEARS_NOTE}")
    print(f"  주가 {pyrs:.1f}년 · 재무 {fyrs:.1f}년 → "
          f"백테스트 가능 기간 약 {usable:.1f}년")

    print("\n  판정:")
    if usable >= 8:
        print("   · 한국(9.5년)과 비교할 만합니다. 그대로 진행하세요.")
    elif usable >= 4:
        print("   · 기간이 짧습니다. t값이 한국의 "
              f"{(usable / 9.5) ** 0.5:.2f}배로 줄어듭니다 — 참고는 되지만")
        print("     '통과/실패'를 단정할 근거는 못 됩니다.")
    else:
        print("   · 너무 짧아 검증으로 쓸 수 없습니다.")
        print("   · yfinance 분기 재무제표가 최근 몇 분기만 주는 게 원인입니다.")
        print("     더 긴 이력이 필요하면 유료 데이터(FMP·Sharadar 등)를 봐야 합니다.")
    if pyrs < 11:
        print(f"   · 주가가 {pyrs:.1f}년뿐입니다. "
              f"QUANT_US_MONTHLY_PRICE_YEARS를 올려 다시 수집하세요 "
              f"(현재 {config.US_MONTHLY_PRICE_YEARS}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
