# -*- coding: utf-8 -*-
"""생존편향이 얼마나 되는지 측정합니다. 백필보다 먼저 돌려야 합니다.

    python tools/probe_survivorship.py

왜 이게 먼저인가
----------------
백테스트는 '지금 상장된 종목'으로 과거를 계산합니다(`_load`가 종목표의 최신
기준일 한 줄만 씁니다). 그 사이 상장폐지된 회사의 -100%가 통째로 빠집니다.

기간을 늘리면 t는 올라갑니다(t ∝ √기간). 그런데 생존편향은 t를 올리는 게
아니라 **추정의 중심을 틀리게** 합니다. 그리고 기간을 늘릴수록 편향이 쌓일
시간도 길어집니다. 즉 백필만 하면:

  · t 2.38 → 3.36 (더 확실해 보임)
  · 편향 ↑  (실제로는 더 틀림)

틀린 곳에 중심이 있는 추정의 신뢰구간을 좁히는 건 위험합니다 — 숫자가
확실해 보여서 자금을 더 넣게 됩니다. 그래서 먼저 재고, 큰지 작은지에 따라
백필을 할지 결정합니다.

판정 기준 (미리 정합니다)
  · 빠진 비율 5% 미만  → 편향 작음. 백필 진행해도 됩니다
  · 5~15%            → 백필 전에 시점별(point-in-time) 유니버스를 만드세요
  · 15% 이상          → 지금 백테스트 숫자부터 크게 할인해야 합니다.
                        그 상태로 기간만 늘리는 건 의미가 없습니다
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import store  # noqa: E402

# 백테스트 시작(2017-03), 중간, 최근 — 편향이 기간과 함께 커지는지 봅니다
CHECK_DATES = ["20170103", "20200102", "20230102", "20250102"]


def main() -> int:
    try:
        from pykrx import stock
    except ImportError:
        print("pykrx가 없습니다: pip install pykrx")
        return 2

    cur = store.read("kor_ticker")
    if cur.empty:
        print("kor_ticker가 비어 있습니다 — 월간 실행을 먼저 돌리세요.")
        return 2
    cur["기준일"] = pd.to_datetime(cur["기준일"], errors="coerce")
    latest = cur[cur["기준일"] == cur["기준일"].max()]
    now_set = set(latest["종목코드"].astype(str).str.zfill(6))
    print(f"현재 종목표: {len(now_set)}종목 (기준일 {latest['기준일'].max():%Y-%m-%d})")
    print(f"종목표에 쌓인 기준일 수: {cur['기준일'].nunique()}개 "
          f"({cur['기준일'].min():%Y-%m} ~ {cur['기준일'].max():%Y-%m})")
    print()

    rows = []
    for d in CHECK_DATES:
        try:
            past = stock.get_market_ticker_list(d, market="ALL")
        except Exception as e:
            print(f"  {d}: 조회 실패 — {type(e).__name__}: {str(e)[:120]}")
            continue
        past_set = {str(c).zfill(6) for c in past}
        gone = past_set - now_set
        rows.append({
            "날짜": f"{d[:4]}-{d[4:6]}",
            "그때 상장": len(past_set),
            "지금 없음": len(gone),
            "빠진 비율": f"{len(gone) / max(len(past_set), 1) * 100:.1f}%",
        })

    if not rows:
        print("한 날짜도 조회하지 못했습니다. KRX 접속(KRX_ID/KRX_PW)을 확인하세요.")
        return 2

    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    worst = max(float(r["빠진 비율"].rstrip("%")) for r in rows)
    print(f"\n  가장 오래된 시점의 누락률: {worst:.1f}%")
    print("\n  판정:")
    if worst < 5:
        print("   · 편향이 작습니다. 기간 연장(백필)을 진행해도 됩니다.")
    elif worst < 15:
        print("   · 편향이 무시할 수 없습니다. 기간을 늘리면 편향도 같이 커집니다.")
        print("   · 백필보다 '시점별 유니버스' 구축이 먼저입니다 —")
        print("     매월 상장 종목 목록을 저장해 두고, 백테스트가 그 시점 목록을")
        print("     쓰게 하면 됩니다. pykrx로 과거 날짜 조회가 가능합니다.")
    else:
        print("   · 편향이 큽니다. 지금 측정치(전종목 대비 +60.79%p, 유동성통과 t 2.38)")
        print("     자체를 크게 할인해야 합니다.")
        print("   · 이 상태로 기간만 늘리는 것은 '틀린 숫자를 더 정밀하게 재는' 일입니다.")
        print("     백필을 하지 마시고 시점별 유니버스부터 만드세요.")

    print("\n  참고: 누락된 종목은 대부분 상장폐지·합병된 회사입니다. 폐지 직전")
    print("  수익률이 크게 음수인 경우가 많아, 빠지면 성과가 '좋아지는' 방향으로")
    print("  편향됩니다. 소형 가치주 전략에서 특히 큽니다 — '싸 보이는 이유가")
    print("  망해가는 중이었다'는 경우가 체계적으로 사라지기 때문입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
