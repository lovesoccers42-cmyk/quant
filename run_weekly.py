# -*- coding: utf-8 -*-
"""한국장 주간 분할 리밸런싱 — 이번 주 주문서를 만듭니다.

    python run_weekly.py                    # 이번 주 주문서
    python run_weekly.py 4                  # 등분 수를 직접 지정
    python run_weekly.py seed 보유종목.csv   # 이미 들고 있는 종목을 등분에 배분

처음 시작할 때 (이미 한국 주식을 들고 있는 경우)
------------------------------------------------
먼저 seed를 한 번 돌리세요. 컬럼 두 개짜리 CSV면 됩니다 (UTF-8):

    종목코드,평가금액,종목명
    005930,8200000,삼성전자
    000660,5400000,SK하이닉스

seed는 보유 종목을 **금액이 고르게** 네 등분에 나눠 넣습니다. 그다음부터
매주 한 등분씩, 모델 상위 100위에서 밀려난 종목만 팔고 그 돈으로 새 종목을
삽니다. 4주면 전환이 끝나고, 한 주에 전 재산을 갈아엎지 않습니다.
seed를 건너뛰면 첫 주문서가 '100종목 신규 매수'로 나옵니다 — 살 돈이 없죠.

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

    r = portfolio.rebalance(picks, n=config.N_PORTFOLIO, tranches=tranches,
                            capital=config.CAPITAL_KRW)

    today = f"{date.today():%Y%m%d}"
    name = (f"{config.REPORT_NAME} 한국 주간주문 {today[4:]}_"
            f"{config.REPORT_SUFFIX}")
    xlsx = config.OUTPUT_DIR / f"{name}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        meta = pd.DataFrame([
            {"항목": "기준일", "값": f"{date.today():%Y-%m-%d}"},
            {"항목": "이번에 손보는 등분",
             "값": ("첫 실행 — 전 등분을 한 번에 채움" if r["첫실행"]
                   else f"{r['이번등분']}번 / 총 {tranches}등분")},
            {"항목": "목표 종목 수", "값": r["목표종목수"]},
            {"항목": "현재 보유", "값": r["보유종목수"]},
            {"항목": "매도", "값": r["매도"]},
            {"항목": "매수", "값": r["매수"]},
            {"항목": "유지", "값": r["유지"]},
            {"항목": "총 운용금액",
             "값": (f"{config.CAPITAL_KRW:,.0f}원" if config.CAPITAL_KRW > 0
                   else "미설정 — QUANT_CAPITAL_KRW를 넣으면 수량까지 계산합니다")},
            {"항목": "종목당 배정액",
             "값": (f"{r['종목당배정액']:,}원" if r.get("종목당배정액")
                   else "수량 계산 안 함")},
            {"항목": "주가가 배정액보다 비싸 제외한 종목",
             "값": (f"{r['가격초과제외']}종목 — 1주도 못 사서 다음 순위로 채웠습니다"
                   if r.get("가격초과제외") else "없음")},
            {"항목": "안내", "값": "아래 '주문' 시트의 종목만 거래하세요. "
                                 "나머지 등분은 이번 주에 손대지 않습니다."},
            {"항목": "매도 수량", "값": "'전량'입니다 — 그 종목을 다 비우세요. "
                                   "보유 수량은 증권사 앱에서 확인하세요."},
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
        "종목당배정액": r.get("종목당배정액"), "가격초과제외": r.get("가격초과제외"),
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
    if r.get("종목당배정액"):
        print(f"  종목당 배정액 {r['종목당배정액']:,}원"
              + (f" · 주가가 비싸 제외 {r['가격초과제외']}종목"
                 if r.get("가격초과제외") else ""))
    if len(r["orders"]):
        print()
        for _, o in r["orders"].iterrows():
            qty = o.get("수량")
            qty_s = (f"{qty:>6,}주" if isinstance(qty, (int, float)) and qty
                     else f"{str(qty or ''):>7}")
            print(f"   {o['구분']}  {o['종목코드']}  {str(o['종목명'])[:16]:<16}"
                  f" {qty_s}  {str(o['섹터'])[:10]}")
    else:
        print("\n  이번 주는 거래할 것이 없습니다.")
    print(f"\n  엑셀: {xlsx.name}")
    print(f"  (모델 선정 {res.get('selected')}종목 중 상위 {config.N_PORTFOLIO}종목 기준)")
    return 0


def seed_main(csv_path: str, tranches: int = 4) -> int:
    """보유 종목 CSV를 읽어 등분에 배분합니다. 전환 시작 전에 한 번만."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    df = pd.read_csv(csv_path, dtype={"종목코드": str}, encoding="utf-8-sig")
    r = portfolio.seed(df, tranches=tranches)

    print("\n" + "=" * 60)
    print("  보유 종목을 등분에 배분했습니다")
    print("=" * 60)
    print(f"  보유 {r['보유종목수']}종목 · 총 {r['총평가액']:,.0f}원")
    for j, (amt, sh) in enumerate(zip(r["등분별금액"], r["등분별비중"])):
        print(f"   {j}번 등분  {amt:>13,}원  ({sh:.1%})")
    if r["금액균형경고"]:
        print(f"\n  [확인] {r['금액균형경고']}")
    print("\n  이제 매주 `python run_weekly.py`를 돌리면 한 등분씩 전환합니다.")
    print(f"  총 운용금액을 환경변수로 넣어두세요: "
          f"QUANT_CAPITAL_KRW={r['총평가액']:.0f}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        if len(sys.argv) < 3:
            print("사용법: python run_weekly.py seed 보유종목.csv [등분수]")
            sys.exit(2)
        sys.exit(seed_main(sys.argv[2],
                           int(sys.argv[3]) if len(sys.argv) > 3 else 4))
    tr = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    sys.exit(main(tr))
