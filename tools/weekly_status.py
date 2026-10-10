# -*- coding: utf-8 -*-
"""주문서 실행 결과를 금액 없이 요약합니다 (공개 릴리스에 올라가는 점검용).

    python tools/weekly_status.py

주문서 json(weekly_kr*_latest.json)에는 종목별 예상금액과 배정액이 들어 있어
비공개 레포로 갑니다. 그런데 토요일 점검은 토큰 없이 공개 API만 읽습니다.
그래서 '제대로 돌았는지'를 판정할 수 있는 날짜와 개수만 따로 뽑아 둡니다.
금액은 '설정됐는지'(참/거짓)만 남기고 숫자는 넣지 않습니다.
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

FILES = {"main": "weekly_kr_latest.json", "alt": "weekly_kr_alt_latest.json"}
KEEP = ("기준일", "주가기준일", "이번등분", "등분수", "첫실행",
        "보유종목수", "미매수", "배정완료종목수",
        "매도", "매수", "유지", "가격초과제외")


def main() -> int:
    out = {"작성일": f"{date.today():%Y-%m-%d}", "계좌": {}}
    for key, fname in FILES.items():
        p = config.OUTPUT_DIR / fname
        if not p.exists():
            out["계좌"][key] = {"없음": f"{fname} 이 없습니다 (주문서가 안 나왔음)"}
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            out["계좌"][key] = {"오류": f"{type(e).__name__}: {str(e)[:120]}"}
            continue
        row = {k: d.get(k) for k in KEEP if k in d}
        # 금액은 숫자를 남기지 않고 '잡혔는지'만 알려 줍니다.
        row["배정액설정됨"] = bool(d.get("종목당배정액"))
        row["주문건수"] = len(d.get("orders") or [])
        종목 = [str(o.get("종목코드", "")).zfill(6)
               for o in (d.get("orders") or []) if o.get("종목코드")]
        row["주문종목수"] = len(set(종목))
        out["계좌"][key] = row

    # 두 계좌가 완전히 같은 종목을 주문했는지 — 분할이 깨졌는지 보는 신호
    try:
        sets = {}
        for key, fname in FILES.items():
            p = config.OUTPUT_DIR / fname
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                sets[key] = {str(o.get("종목코드", "")).zfill(6)
                             for o in (d.get("orders") or [])
                             if o.get("종목코드")}
        if len(sets) == 2 and all(sets.values()):
            a, b = sets["main"], sets["alt"]
            out["두계좌_주문종목_동일"] = (a == b)
            out["두계좌_겹침비율"] = round(len(a & b) / max(len(a | b), 1), 3)
    except Exception:
        pass

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    q = config.OUTPUT_DIR / "weekly_status.json"
    q.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  주문서 요약 (금액 없음) · {out['작성일']}")
    print("=" * 60)
    for key, a in out["계좌"].items():
        if "없음" in a or "오류" in a:
            print(f"  {key}: {a.get('없음') or a.get('오류')}")
            continue
        print(f"  {key}: 기준일 {a.get('기준일')} · 주가 {a.get('주가기준일')} "
              f"· 등분 {a.get('이번등분')}/{a.get('등분수')}")
        print(f"       보유 {a.get('보유종목수')}"
              + (f" + 미매수 {a.get('미매수')}" if a.get("미매수") else "")
              + f" · 매도 {a.get('매도')} · 매수 {a.get('매수')}"
              f" · 가격초과제외 {a.get('가격초과제외')}"
              f" · 배정액 {'설정됨' if a.get('배정액설정됨') else '없음'}")
    if "두계좌_겹침비율" in out:
        print(f"  두 계좌 주문 겹침 {out['두계좌_겹침비율']:.0%}"
              + (" — 동일! 분할 확인 필요" if out["두계좌_주문종목_동일"] else ""))
    print(f"\n  {q.name} 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
