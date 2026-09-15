# -*- coding: utf-8 -*-
"""DART 과거 재무제표 채우기 — 하루 한도만큼 받고 멈춥니다.

    python run_dart.py            # 기본 예산(19,000회)
    python run_dart.py 5000       # 시험 삼아 5,000회만
    python run_dart.py 5000 2020  # 2020년부터

하루 한도(20,000회)에 걸리면 그 지점을 기록하고 끝냅니다.
다음 날 같은 명령을 다시 돌리면 이어서 받습니다.
"""
import logging
import sys
from datetime import date

import config
import kor_fs_dart
import store


def main(budget: int = 0, start_year: int = 0) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"dart_{date.today():%Y%m%d}.log", encoding="utf-8"),
            logging.StreamHandler()], force=True)

    budget = budget or config.DART_DAILY_BUDGET
    years = range(start_year or config.DART_START_YEAR,
                  date.today().year + 1)

    before = store.read_sql(
        "select min(기준일) 처음, max(기준일) 마지막, count(*) 행 from kor_fs "
        "where 공시구분 = 'q';")

    res = kor_fs_dart.collect(budget=budget, years=years)

    after = store.read_sql("""
        select date_trunc('year', 기준일) 연도, count(distinct 종목코드) 종목수
        from kor_fs where 공시구분 = 'q' group by 1 order by 1;
    """)

    diag = res.pop("진단", None)

    print("\n" + "=" * 58)
    print("  DART 과거 재무제표 수집")
    print("=" * 58)
    for k, v in res.items():
        print(f"  {k:<10} {v}")

    if diag:
        # 응답은 정상인데 계정을 하나도 못 찾았습니다. 실제로 무엇이 왔는지
        # 그대로 찍어야 매핑을 고칠 수 있습니다.
        print("\n  [진단] 응답은 정상인데 아는 계정이 없습니다.")
        print(f"  {diag['종목코드']} · {diag['연도']}년 · 보고서 {diag['보고서']} "
              f"· 총 {diag['행수']}행")
        print(f"  {'account_id':<45} {'sj':<4} {'account_nm':<22} 금액")
        for r in diag["예시"]:
            print(f"  {str(r.get('account_id'))[:45]:<45} "
                  f"{str(r.get('sj_div'))[:4]:<4} "
                  f"{str(r.get('account_nm'))[:22]:<22} "
                  f"{str(r.get('thstrm_amount'))[:18]} | "
                  f"{str(r.get('thstrm_add_amount'))[:18]} | "
                  f"{str(r.get('thstrm_dt'))[:24]}")
        print("\n  위 표를 그대로 복사해서 보내주세요.")
    print(f"\n  수집 전: {before.iloc[0]['처음']} ~ {before.iloc[0]['마지막']} "
          f"({int(before.iloc[0]['행']):,}행)")
    print("\n  연도별 종목 수:")
    for _, r in after.iterrows():
        print(f"   · {r['연도']:%Y}  {int(r['종목수']):,}종목")

    if res["남은작업"] > 0:
        print(f"\n  아직 {res['남은작업']:,}건 남았습니다. 내일 다시 돌리면 이어서 받습니다.")
    else:
        print("\n  전부 받았습니다. 이제 백테스트 기간을 늘려 돌려보세요.")
    return 0


if __name__ == "__main__":
    b = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    y = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    sys.exit(main(b, y))
