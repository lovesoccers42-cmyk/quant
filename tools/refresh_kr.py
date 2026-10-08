# -*- coding: utf-8 -*-
"""주간 주문서를 만들기 직전에 최신 주가·시총을 받아옵니다 (리포트 발송 없음).

    python tools/refresh_kr.py

왜 필요한가
-----------
주간 워크플로는 저장소에 있는 주가로 모델을 다시 돌립니다. 그 주가를 채우는
건 일간 실행(daily.yml)인데, 둘의 cron이 같은 분(07:30 UTC)에 걸려 있고
GitHub가 예약 실행을 6~9시간씩 밀어서 돌립니다(실측: 13:57 ~ 16:12 UTC).
같은 concurrency 그룹이라 순서대로 돌긴 하지만 **어느 쪽이 먼저인지는
보장되지 않습니다.** 주간이 먼저 돌면 전날 종가로 주문서가 만들어집니다.

순서에 의존하지 않도록, 주문서를 만들기 직전에 직접 받아옵니다. 받는 것은
일간 실행의 데이터 단계와 같습니다:

  · 티커·시가총액·종가 (pykrx)   — 밸류 지표와 지수 벤치마크가 이걸 씁니다
  · 최근 주가 (config.DAILY_PRICE_DAYS일)
  · 밸류 지표 재계산

섹터와 재무제표는 월간 실행이 채웁니다(자주 바뀌지 않습니다). QVM 모델은
run_weekly가 계좌별로 다시 돌리므로 여기서 돌리지 않습니다.

실패해도 주문서는 나와야 합니다(저장된 주가로). 그래서 종료코드는 항상 0이고,
무엇이 실패했는지만 남깁니다. 대신 주문서 요약에 '주가 기준일'을 적어 두었으니
거래 전에 그 날짜가 맞는지 보시면 됩니다.
"""
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import config  # noqa: E402
import http_util  # noqa: E402
import kor_price  # noqa: E402
import kor_ticker  # noqa: E402
import kor_value  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402

log = logging.getLogger("quant_agent.refresh")


def _price_asof() -> str:
    try:
        df = store.read("kor_price")
        if df.empty:
            return "없음"
        return f"{pd.to_datetime(df['날짜']).max():%Y-%m-%d}"
    except Exception:
        return "확인 실패"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"refresh_{date.today():%Y%m%d}.log",
            encoding="utf-8"), logging.StreamHandler()], force=True)
    http_util.install_log_redaction()

    before = _price_asof()
    print(f"  갱신 전 주가 기준일: {before}")

    store.init_db(market="kr")
    biz = pipeline._biz_day()
    failed = []

    for label, fn, kw in (
        ("티커·시총", kor_ticker.collect, {"biz_day": biz}),
        (f"주가 {config.DAILY_PRICE_DAYS}일", kor_price.collect,
         {"days": config.DAILY_PRICE_DAYS}),
        ("밸류 지표", kor_value.build, {}),
    ):
        try:
            stats = fn(**kw)
            print(f"  [OK] {label} — {stats}")
        except Exception as e:
            failed.append(label)
            log.error("%s 실패 — %s: %s", label, type(e).__name__, str(e)[:200])
            print(f"  [실패] {label} — {type(e).__name__}: {str(e)[:150]}")

    after = _price_asof()
    print(f"  갱신 후 주가 기준일: {after} (기준일 {biz})")
    if failed:
        print(f"  ::warning::갱신 실패 {', '.join(failed)} — "
              f"저장된 주가({after})로 주문서를 만듭니다. "
              f"요약의 '주가 기준일'을 확인하세요.")
    return 0            # 실패해도 주문서는 만들어야 합니다


if __name__ == "__main__":
    sys.exit(main())
