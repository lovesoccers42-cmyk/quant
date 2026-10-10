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
    cap_date, cap_pledged = None, 0.0
    if not cap.empty:
        cap_date = f"{pd.to_datetime(cap['기준일']).max():%Y-%m-%d}"
        cap_pledged = float(portfolio.load_capital(prof["capital_table"])
                            .get("약정액") or 0.0)

    log = portfolio.trade_log(account=key)
    last_order, filled, unfilled, marked, weeks = None, 0, 0, 0, 0
    if not log.empty:
        weeks = int(log["기준일"].nunique())
        last = log[log["기준일"] == log["기준일"].max()]
        last_order = f"{log['기준일'].max():%Y-%m-%d}"
        filled = int(last["체결가"].notna().sum())
        unfilled = int(last["체결가"].isna().sum())
        # 체결가가 빈 행은 두 가지입니다 — '체결내역을 아직 안 넣었다'와
        # '넣었고 거기 없었다(= 안 일어난 거래)'. 표시가 있는 쪽만 센 숫자가
        # 있어야 요약만 보고 구분할 수 있습니다. 이게 없어서 "됐나요?"를
        # 같은 JSON으로 세 번 묻게 됐습니다.
        memo = last["메모"].astype(str) if "메모" in last.columns else pd.Series(dtype=str)
        marked = int(memo.str.contains("미체결", na=False).sum())

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
        "원장_미체결표시": marked,       # 표시가 붙은 미체결 (= 체결내역 반영됨)
        "원장_기록주차수": weeks,
        "약정액설정": bool(cap_pledged), # 금액은 적지 않습니다 — 설정 여부만
    }


def _checks(key: str, a: dict, today: date) -> list:
    """요약만 보고 '됐나'를 판정할 수 있게 경고를 글로 적습니다.

    숫자를 늘리는 것만으로는 부족합니다 — 체결내역을 안 넣은 주와 넣은 주의
    숫자가 똑같이 나오면(미체결 4건) 요약을 봐도 알 수 없습니다.
    """
    out = []
    if a.get("오류"):
        return [f"{key}: 오류 — {a['오류']}"]
    if a["원장_최근주문일"] and a["원장_체결반영"] == 0 and a["원장_미체결"]:
        out.append(f"{key}: {a['원장_최근주문일']} 주문의 체결내역이 "
                   f"안 들어왔습니다 (미체결 {a['원장_미체결']}건) — "
                   f"inbox에 fills_{key}_*.csv 를 올리세요")
    elif a["원장_미체결"] > a["원장_미체결표시"]:
        out.append(f"{key}: 미체결 {a['원장_미체결']}건 중 "
                   f"{a['원장_미체결'] - a['원장_미체결표시']}건에 표시가 "
                   f"없습니다 — 수익률 계산이 그 건을 계획가에 거래한 것으로 "
                   f"읽습니다. 체결내역을 다시 적용하세요")
    if not a["약정액설정"]:
        out.append(f"{key}: 약정 운용금액이 설정되지 않았습니다 — "
                   f"예수금이 0이면 배정액이 쪼그라듭니다")
    if a["자본기준일"]:
        age = (today - pd.Timestamp(a["자본기준일"]).date()).days
        if age > 10:
            out.append(f"{key}: 마지막 재동기화가 {age}일 전입니다 — "
                       f"매도 주문을 믿기 전에 보유목록을 올리세요")
    else:
        out.append(f"{key}: 자본 기록이 없습니다")
    return out


def main() -> int:
    today = date.today()
    out = {"작성일": f"{today:%Y-%m-%d}", "계좌": {}}
    for key in config.PROFILES:
        try:
            out["계좌"][key] = _acct(key)
        except Exception as e:
            out["계좌"][key] = {"오류": f"{type(e).__name__}: {str(e)[:120]}"}
    점검 = []
    for key, a in out["계좌"].items():
        점검 += _checks(key, a, today)
    out["점검"] = 점검
    out["판정"] = "이상 없음" if not 점검 else f"확인 필요 {len(점검)}건"

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
                  f"체결반영 {a['원장_체결반영']} / 미체결 {a['원장_미체결']}"
                  f" (표시 {a['원장_미체결표시']})")
    print(f"\n  판정: {out['판정']}")
    for line in out["점검"]:
        print(f"    - {line}")
    print(f"\n  {p.name} 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
