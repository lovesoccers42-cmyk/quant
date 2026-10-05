# -*- coding: utf-8 -*-
"""한국장 주간 분할 리밸런싱 — 이번 주 주문서를 만듭니다.

    python run_weekly.py

백테스트에서 확정된 설정(100종목 · 주간 · 4등분)을 실전에 적용합니다.
모델을 새로 돌려 순위를 갱신한 뒤, 보유 중인 네 등분 중 **한 등분만**
손봅니다. 매주 바뀌는 종목은 보통 2~5개입니다.

왜 전체를 안 갈아엎나: 매주 100종목을 다 교체하면 연 회전율이 600%를
넘고, 백테스트에서 그 방식은 초과수익 -12%p였습니다. 한 등분만 손대면
회전율이 월간과 같은 수준(연 211%)으로 유지되면서 +16%p가 나왔습니다.
"""
import json
import logging
import sys
from datetime import date

import pandas as pd

import config
import factor_kor
import http_util
import portfolio
import store

log = logging.getLogger("quant_agent.weekly")


def main(tranches: int = 4, dry_run: bool = False) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"weekly_{date.today():%Y%m%d}.log", encoding="utf-8"),
            logging.StreamHandler()], force=True)
    http_util.install_log_redaction()

    log.info("모델 실행 — 순위 갱신")
    res = factor_kor.run()
    picks = pd.read_parquet(config.OUTPUT_DIR / "model_kr_latest.parquet")
    if picks.empty:
        raise RuntimeError("모델 결과가 비어 있습니다.")

    r = portfolio.rebalance(picks, n=config.N_PORTFOLIO, tranches=tranches)

    today = f"{date.today():%Y%m%d}"
    name = (f"{config.REPORT_NAME} 한국 주간주문 {today[4:]}_"
            f"{config.REPORT_SUFFIX}")
    xlsx = config.OUTPUT_DIR / f"{name}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        meta = pd.DataFrame([
            {"항목": "기준일", "값": f"{date.today():%Y-%m-%d}"},
            {"항목": "이번에 손보는 등분", "값": f"{r['이번등분']} / {tranches}등분"},
            {"항목": "목표 종목 수", "값": r["목표종목수"]},
            {"항목": "현재 보유", "값": r["보유종목수"]},
            {"항목": "매도", "값": r["매도"]},
            {"항목": "매수", "값": r["매수"]},
            {"항목": "유지", "값": r["유지"]},
            {"항목": "안내", "값": "아래 '주문' 시트의 종목만 거래하세요. "
                                 "나머지 등분은 이번 주에 손대지 않습니다."},
        ])
        meta.to_excel(xw, sheet_name="요약", index=False)
        (r["orders"] if len(r["orders"])
         else pd.DataFrame([{"구분": "없음", "종목코드": "", "종목명": "이번 주 거래 없음"}])
         ).to_excel(xw, sheet_name="주문", index=False)
        r["holdings"].to_excel(xw, sheet_name="보유현황", index=False)

    (config.OUTPUT_DIR / "weekly_kr_latest.json").write_text(json.dumps({
        "기준일": f"{date.today():%Y-%m-%d}", "이번등분": r["이번등분"],
        "등분수": tranches, "매도": r["매도"], "매수": r["매수"], "유지": r["유지"],
        "보유종목수": r["보유종목수"], "첫실행": r["첫실행"],
        "orders": r["orders"].to_dict(orient="records"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  한국장 주간 주문서 · {date.today():%Y-%m-%d}")
    print("=" * 60)
    if r["첫실행"]:
        print(f"  첫 실행입니다 — {r['매수']}종목을 한 번에 담습니다.")
    else:
        print(f"  {tranches}등분 중 {r['이번등분']}번 등분 차례")
        print(f"  매도 {r['매도']} · 매수 {r['매수']} · 유지 {r['유지']}")
    if len(r["orders"]):
        print()
        for _, o in r["orders"].iterrows():
            print(f"   {o['구분']}  {o['종목코드']}  {str(o['종목명'])[:18]:<18}"
                  f" {str(o['섹터'])[:10]}")
    else:
        print("\n  이번 주는 거래할 것이 없습니다.")
    print(f"\n  엑셀: {xlsx.name}")
    print(f"  (모델 선정 {res.get('selected')}종목 중 상위 {config.N_PORTFOLIO}종목 기준)")
    return 0


if __name__ == "__main__":
    tr = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    sys.exit(main(tr))
