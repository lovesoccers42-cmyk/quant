# -*- coding: utf-8 -*-
"""재무제표 정합성 점검 — 숫자가 믿을 만한지 눈으로 확인합니다.

왜 필요한가
-----------
같은 기간(2023-10~2026-09)을 같은 조건으로 돌렸는데, FnGuide 재무제표일 때는
초과수익 +18%p, DART 재무제표로 바뀐 뒤에는 -12%p가 나왔습니다. 모델은 그대로고
데이터만 바뀌었으니, 둘 중 하나(또는 둘 다)가 틀렸다는 뜻입니다. 어느 쪽이
틀렸는지 모르면 백테스트 결과를 하나도 믿을 수 없습니다.

그래서 값 자체를 봅니다. 삼성전자·SK하이닉스 같은 대형주의 분기 매출과 순이익은
공개된 숫자라 맞는지 바로 알 수 있습니다.

    python tools/check_fs.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402

ACCOUNTS = ["매출액", "매출총이익", "당기순이익", "영업활동으로인한현금흐름", "자본", "자산"]
SAMPLE = {"005930": "삼성전자", "000660": "SK하이닉스", "005380": "현대차"}


def main() -> int:
    fs = store.read("kor_fs")
    if fs.empty:
        print("kor_fs가 비어 있습니다.")
        return 1
    q = fs[fs["공시구분"] == "q"].copy()
    q["기준일"] = pd.to_datetime(q["기준일"])

    print("=" * 76)
    print("  재무제표 정합성 점검")
    print("=" * 76)
    print(f"  기간 {q['기준일'].min():%Y-%m-%d} ~ {q['기준일'].max():%Y-%m-%d} · "
          f"{len(q):,}행 · 종목 {q['종목코드'].nunique():,}개")
    if "출처" in q.columns:
        print("  출처별 행 수:", q["출처"].fillna("(미기록)").value_counts().to_dict())
    else:
        print("  출처 컬럼 없음 — 이번 수정 이후 수집분부터 기록됩니다")

    # ── 1. 대형주 분기 값 (공개된 숫자와 대조용) ──────────────
    for code, name in SAMPLE.items():
        sub = q[(q["종목코드"] == code) & (q["계정"].isin(ACCOUNTS))
                & (q["기준일"] >= "2024-01-01")]
        if sub.empty:
            print(f"\n  [{name}] 자료 없음")
            continue
        piv = sub.pivot_table(index="기준일", columns="계정", values="값", aggfunc="last")
        piv = piv.reindex(columns=[c for c in ACCOUNTS if c in piv.columns])
        print(f"\n  [{name} {code}] 단위: 억원 — 분기(3개월) 값이어야 합니다")
        print(piv.round(0).to_string())

    # ── 2. 말이 안 되는 값 ───────────────────────────────────
    piv = q[q["계정"].isin(ACCOUNTS)].pivot_table(
        index=["종목코드", "기준일"], columns="계정", values="값", aggfunc="last")
    bad = {}
    if {"매출총이익", "매출액"} <= set(piv.columns):
        bad["매출총이익 > 매출액"] = int((piv["매출총이익"] > piv["매출액"] * 1.001).sum())
    for c in ("자산", "자본"):
        if c in piv.columns:
            bad[f"{c} ≤ 0"] = int((piv[c] <= 0).sum())
    missing = piv[ACCOUNTS].isna().any(axis=1).mean() if set(ACCOUNTS) <= set(piv.columns) else None
    print("\n  [이상값]")
    for k, v in bad.items():
        print(f"   · {k}: {v:,}건")
    if missing is not None:
        print(f"   · 6개 계정이 다 있지는 않은 (종목·분기) 비율: {missing:.1%}")

    # ── 3. 자본이 분기마다 급변하는가 (연결/별도 혼재 신호) ───
    # 자본총계는 분기 사이에 몇십 %씩 뛰지 않습니다. 연결 재무제표와 별도
    # 재무제표가 한 종목 안에서 섞이면 계단처럼 튑니다.
    if "자본" in piv.columns:
        eq = piv["자본"].unstack(level=0) if isinstance(piv.index, pd.MultiIndex) else None
        cap = q[q["계정"] == "자본"].pivot_table(
            index="기준일", columns="종목코드", values="값", aggfunc="last").sort_index()
        chg = cap.pct_change().abs()
        jump = (chg > 0.5).sum(axis=1)
        tot = cap.notna().sum(axis=1).replace(0, pd.NA)
        rate = (jump / tot).dropna()
        print("\n  [자본총계가 직전 분기 대비 50% 이상 튄 종목 비율]")
        print("   (정상이면 2~3% 이하. 10%를 넘는 분기는 연결/별도 혼재를 의심)")
        for d, r in rate.tail(16).items():
            flag = "  ← 의심" if r > 0.10 else ""
            print(f"   · {d:%Y-%m-%d}  {r:5.1%}{flag}")

    # ── 4. 분기별 커버리지 ───────────────────────────────────
    cov = q[q["계정"].isin(ACCOUNTS)].groupby(
        [q["기준일"].dt.to_period("Q"), "계정"])["종목코드"].nunique().unstack()
    print("\n  [분기별 계정 커버리지 — 종목 수]")
    print(cov.tail(14).fillna(0).astype(int).to_string())
    print("\n  계정끼리 종목 수가 크게 다르면 한 분기 안에서 계정마다 출처가")
    print("  다를 수 있습니다. ROE=당기순이익/자본처럼 두 계정을 나누는 지표가 망가집니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
