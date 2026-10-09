# -*- coding: utf-8 -*-
"""데이터 관리 실행의 결과를 한 장으로 요약합니다 (금액 없음).

    python tools/manage_status.py

왜 금액을 안 적는가: 이 파일은 공개 릴리스에 올라가 주간 점검이 읽습니다.
평가액·총자본이 들어가면 자산 규모가 공개됩니다. 점검에 필요한 건 '맞춰졌는지'
이지 '얼마인지'가 아니므로 날짜와 개수만 적습니다.
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import config  # noqa: E402
import portfolio  # noqa: E402
import store  # noqa: E402


def _acct(key: str) -> dict:
    prof = config.profile(key)
    st = portfolio.load_state(prof["state_table"])
    base = pd.to_numeric(st["기준금액"], errors="coerce") if len(st) else pd.Series(dtype=float)
    pending = int((base == 0).sum())
    cap = store.read(prof["capital_table"])
    cap_date = None
    if not cap.empty:
        cap_date = f"{pd.to_datetime(cap['기준일']).max():%Y-%m-%d}"

    log = portfolio.trade_log(account=key)
    last_order, filled, unfilled, weeks = None, 0, 0, 0
    if not log.empty:
        weeks = int(log["기준일"].nunique())
        last = log[log["기준일"] == log["기준일"].max()]
        last_order = f"{log['기준일'].max():%Y-%m-%d}"
        filled = int(last["체결가"].notna().sum())
        unfilled = int(last["체결가"].isna().sum())

    return {
        "보유종목수": int(len(st) - pending),
        "미매수": pending,
        "배정완료종목수": int(len(st)),
        "등분별종목수": ({int(k): int(v) for k, v in
                      st["등분"].value_counts().sort_index().items()}
                     if len(st) else {}),
        "자본기준일": cap_date,          # 마지막 resync·운용금액 설정 날짜
        "자본기록수": int(len(cap)),      # 성과 곡선의 점 개수
        "원장_최근주문일": last_order,
        "원장_체결반영": filled,
        "원장_미체결": unfilled,
        "원장_기록주차수": weeks,
    }


def main() -> int:
    out = {"작성일": f"{date.today():%Y-%m-%d}", "계좌": {}}
    for key in config.PROFILES:
        try:
            out["계좌"][key] = _acct(key)
        except Exception as e:
            out["계좌"][key] = {"오류": f"{type(e).__name__}: {str(e)[:120]}"}

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = config.OUTPUT_DIR / "manage_latest.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  데이터 관리 결과 · {out['작성일']}")
    print("=" * 60)
    for key, a in out["계좌"].items():
        if "오류" in a:
            print(f"  {key}: 오류 — {a['오류']}")
            continue
        print(f"  {key}: 보유 {a['보유종목수']}종목"
              + (f" + 미매수 {a['미매수']}" if a["미매수"] else "")
              + f" · 등분 {a['등분별종목수']}")
        print(f"       자본기준일 {a['자본기준일']} (기록 {a['자본기록수']}점)"
              f" · 원장 {a['원장_기록주차수']}주차")
        if a["원장_최근주문일"]:
            print(f"       최근주문 {a['원장_최근주문일']} — "
                  f"체결반영 {a['원장_체결반영']} / 미체결 {a['원장_미체결']}")
    print(f"\n  {p.name} 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
