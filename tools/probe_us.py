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


def model_universe(fs: pd.DataFrame) -> None:
    """과거 시점마다 '모델이 실제로 점수를 낼 수 있는 종목 수'.

    위의 '4분기 이상 N종목'은 계정을 섞어 센 숫자라 과대평가입니다. 모델은
    여섯 계정이 **각각** 유효한 TTM을 가져야 점수를 냅니다. 그걸 안 재서
    '585종목'이라고 믿고 미국 백테스트를 두 번 돌렸는데, 실제 회차당
    점수산출종목은 평균 25개(범위 19~63)였습니다.

    계정별 수도 같이 찍습니다 — 하나가 막으면 교집합이 통째로 무너지고,
    어느 계정인지는 교집합 숫자만 봐서는 알 수 없기 때문입니다.
    (실측 2026-10-15: 2022-01 교집합 37종목, 영업현금흐름만 66종목,
     그것만 빼면 1,709종목이었습니다.)
    """
    import backtest as bt
    import factor_core as fc

    spec = bt.SPECS["us"]
    accts = sorted({a for pair in spec.quality.values() for a in pair}
                   | set(spec.value.values()))
    q = fs[(fs["freq"] == "q") & (fs["account"].isin(accts))]
    if q.empty:
        print("\n  여섯 계정 중 아무것도 없습니다 — 수집부터 다시 보세요.")
        return

    today = pd.Timestamp(date.today())
    cutoffs = [c for c in pd.date_range(end=today.normalize(), periods=6,
                                        freq="YS") if c >= q["date"].min()]
    cutoffs = (cutoffs or [today])[-5:] + [today]

    print("\n  시점별 '여섯 계정 모두 유효한 TTM' 종목 수"
          "  ← 모델이 실제로 점수를 낼 수 있는 모수")
    head = "    " + "시점".ljust(12) + "모두".rjust(7) + "".join(
        a[:14].rjust(16) for a in accts)
    print(head)
    for c in cutoffs:
        t = fc.latest_ttm(q[q["date"] <= c], symbol="Symbol",
                          account="account", date="date", value="value",
                          mean_accounts=spec.mean_accounts)
        t = t[t["ttm"].notna()]
        per = {a: set(t.loc[t["account"] == a, "Symbol"]) for a in accts}
        both = set.intersection(*per.values()) if per else set()
        line = "    " + f"{c:%Y-%m}".ljust(12) + f"{len(both):,}".rjust(7)
        for a in accts:
            line += f"{len(per[a]):,}".rjust(16)
        print(line)

    worst = min(accts, key=lambda a: len(per[a]))
    print(f"\n    가장 좁은 계정: {worst} ({len(per[worst]):,}종목)")
    if len(both) and len(per[worst]) < len(both) * 2:
        print("    → 이 계정이 모수를 결정합니다. 여기가 비면 나머지 다섯이 "
              "멀쩡해도 백테스트가 못 돕니다.")
    if len(both) < 200:
        print(f"    → 200종목 포트폴리오를 담을 수 없습니다 "
              f"(현재 {len(both):,}종목). 백테스트 결과를 믿지 마세요.")


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

    model_universe(fs)

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
