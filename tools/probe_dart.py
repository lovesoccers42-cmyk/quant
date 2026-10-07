# -*- coding: utf-8 -*-
"""DART 원본 응답을 그대로 찍어 어디서 값이 틀어지는지 봅니다.

    python tools/probe_dart.py 005930 2026
    python tools/probe_dart.py 000660 2026

왜 필요한가
-----------
2026년 값이 말이 안 됩니다. SK하이닉스 2026-06-30은 당기순이익이 매출액보다
크고(118%), 자본총계가 반년 만에 두 배가 됐습니다. 둘 다 불가능합니다.
반면 현대차는 멀쩡하고, 2024~2025년은 전 종목이 멀쩡합니다.

그래서 추측 대신 DART가 실제로 뭘 주는지, 우리가 그걸 어떻게 바꾸는지를
나란히 놓고 봅니다. 한 종목·한 해면 8회 호출이면 됩니다.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import http_util  # noqa: E402
import kor_fs_dart as dart  # noqa: E402

NAMES = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업(연간)"}


def main(ticker: str, year: int) -> int:
    logging.basicConfig(level=logging.WARNING, force=True)
    http_util.install_log_redaction()

    key = config.DART_API_KEY
    if not key:
        print("DART_API_KEY가 없습니다.")
        return 1

    corp = dart.corp_map(key).get(ticker)
    if not corp:
        print(f"{ticker}: DART corp_code를 찾지 못했습니다.")
        return 1

    print("=" * 96)
    print(f"  DART 원본 응답 — {ticker} · {year}년")
    print("=" * 96)

    by_q = {}
    for reprt, q, _m in dart.REPORTS:
        data, used = dart.fetch_report(key, corp, year, reprt)
        st = str(data.get("status"))
        print(f"\n[{NAMES[reprt]} / {reprt}] status={st} · 호출 {used}회 · "
              f"행 {len(data.get('list') or [])}")
        if st != "000":
            continue

        rows = data["list"]
        print(f"  {'계정':<22}{'sj':<5}{'account_id':<42}"
              f"{'thstrm_amount':>20}{'add_amount':>20}")
        for account, (ids, names) in dart.TARGETS.items():
            r = dart._pick(rows, ids, names, dart.PREFERRED_SJ.get(account, ()))
            if r is None:
                print(f"  {account:<22}{'-':<5}{'(못 찾음)':<42}")
                continue
            print(f"  {account:<22}{str(r.get('sj_div')):<5}"
                  f"{str(r.get('account_id'))[:42]:<42}"
                  f"{str(r.get('thstrm_amount'))[:20]:>20}"
                  f"{str(r.get('thstrm_add_amount') or '-')[:20]:>20}")

        parsed = dart.parse_report(data, ticker, q, year, reprt)
        if parsed:
            by_q[q] = parsed
            print("  → 해석(억원, 기준):", {a: (round(v, 0), b)
                                       for a, (_, v, b) in parsed.items()})

    print("\n" + "-" * 96)
    print("  최종 저장될 분기값 (억원)")
    print("-" * 96)
    df = dart.to_quarterly(by_q, ticker)
    if len(df):
        piv = df.pivot_table(index="기준일", columns="계정", values="값", aggfunc="last")
        print(piv.round(0).to_string())
    else:
        print("  (저장할 행 없음)")
    return 0


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "005930"
    y = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    sys.exit(main(t, y))
