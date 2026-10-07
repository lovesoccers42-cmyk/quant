# -*- coding: utf-8 -*-
"""한국장 주간 분할 리밸런싱 — 이번 주 주문서를 만듭니다.

    python run_weekly.py                    # 이번 주 주문서
    python run_weekly.py 4                  # 등분 수를 직접 지정
    python run_weekly.py seed 보유종목.csv   # 처음 한 번 — 보유 종목을 등분에 배분
    python run_weekly.py resync 보유종목.csv 350000   # 월 1회 — 평가액·현금 갱신

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

    hold_path = config.OUTPUT_DIR / "model_kr_hold.parquet"
    hold_universe = None
    if hold_path.exists():
        hold_universe = (pd.read_parquet(hold_path)["종목코드"]
                         .astype(str).str.zfill(6).tolist())
        log.info("순위 버퍼 명단 %d종목 사용", len(hold_universe))
    else:
        log.warning("model_kr_hold.parquet이 없어 순위 버퍼 없이 돌립니다 "
                    "— 백테스트와 규칙이 달라집니다")

    # 총자본 — 월 1회 재동기화 값이 있으면 그걸 씁니다 (환경변수는 폴백).
    cap_info = portfolio.load_capital()
    capital = float(cap_info.get("총자본") or config.CAPITAL_KRW)
    cash = float(cap_info.get("현금") or 0.0)
    if cap_info:
        log.info("재동기화 기준 총자본 %.0f원 (평가액 %.0f + 현금 %.0f) · %s",
                 capital, cap_info.get("평가액", 0), cash,
                 pd.Timestamp(cap_info["기준일"]).date())
    else:
        log.warning("재동기화 기록이 없습니다 — QUANT_CAPITAL_KRW(%.0f)를 씁니다. "
                    "`run_weekly.py resync`를 한 번 돌리세요.", config.CAPITAL_KRW)

    r = portfolio.rebalance(picks, n=config.N_PORTFOLIO, tranches=tranches,
                            capital=capital, cash=cash,
                            hold_universe=hold_universe)

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
            {"항목": "매도 (전량)", "값": r["매도"]},
            {"항목": "비중조절 (일부만)", "값": r.get("비중조절", 0)},
            {"항목": "현금투입",
             "값": (f"{r.get('현금투입', 0)}종목 / "
                   f"{r.get('현금투입금액', 0):,}원" if r.get("현금투입")
                   else "없음")},
            {"항목": "매수", "값": r["매수"]},
            {"항목": "유지 (손 안 댐)", "값": r["유지"] - r.get("비중조절", 0)},
            {"항목": "총 운용금액",
             "값": (f"{capital:,.0f}원" if capital > 0
                   else "미설정 — resync를 돌리거나 QUANT_CAPITAL_KRW를 넣으세요")},
            {"항목": "투입 대기 현금",
             "값": (f"{cash:,.0f}원 (이번 주 예산 "
                   f"{r.get('이번주현금예산', 0):,}원)" if cash > 0 else "없음")},
            {"항목": "종목당 배정액",
             "값": (f"{r['종목당배정액']:,}원" if r.get("종목당배정액")
                   else "수량 계산 안 함")},
            {"항목": "주가가 배정액보다 비싸 제외한 종목",
             "값": (f"{r['가격초과제외']}종목 — 1주도 못 사서 다음 순위로 채웠습니다"
                   if r.get("가격초과제외") else "없음")},
            {"항목": "비중축소로 확보되는 금액",
             "값": (f"{r['확보금액_하한']:,}원 이상 (전량매도 대금은 별도)"
                   if r.get("확보금액_하한") else "-")},
            {"항목": "매수에 필요한 금액",
             "값": (f"{r['필요금액']:,}원" if r.get("필요금액") else "-")},
            {"항목": "안내", "값": "아래 '주문' 시트의 종목만 거래하세요. "
                                 "나머지 등분은 이번 주에 손대지 않습니다."},
            {"항목": "구분 읽는 법",
             "값": "매도=전량 비우기 · 비중축소/비중확대=적힌 수량만큼만 "
                  "(종목은 계속 보유) · 현금투입=쌓인 현금으로 추가 매수 "
                  "· 매수=신규 편입"},
            {"항목": "돈이 모자라면",
             "값": "매도·비중축소를 먼저 체결하고, 들어온 현금으로 살 수 있는 "
                  "만큼만 위에서부터 사세요. 못 산 종목은 다음 차례에 채웁니다. "
                  "다른 등분을 건드려 돈을 만들지는 마세요."},
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


def seed_main(src: str, tranches: int = 4) -> int:
    """보유 종목 목록을 읽어 등분에 배분합니다. 전환 시작 전에 한 번만.

    src는 파일 경로여도 되고, 엑셀에서 복사한 문자열을 그대로 줘도 됩니다
    (탭 구분, 줄바꿈이 사라진 상태여도 복원합니다).
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    parsed = portfolio.parse_holdings(src)
    print(f"\n  읽어낸 보유 종목 {len(parsed)}개 · "
          f"합계 {parsed['평가금액'].sum():,.0f}원")
    print("  (앞 3개 / 뒤 3개로 제대로 읽혔는지 확인하세요)")
    for _, h in pd.concat([parsed.head(3), parsed.tail(3)]).iterrows():
        print(f"    {h['종목코드']}  {h['평가금액']:>12,.0f}원  {h['종목명']}")

    r = portfolio.seed(parsed, tranches=tranches)

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


def resync_main(src: str, cash: float = 0.0, tranches: int = 4) -> int:
    """월 1회 — 실제 보유 평가액으로 기준금액과 총자본을 다시 맞춥니다."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    r = portfolio.resync(src, cash=cash, tranches=tranches)
    print("\n" + "=" * 60)
    print("  재동기화 완료")
    print("=" * 60)
    print(f"  보유 {r['보유종목수']}종목")
    print(f"  평가액 {r['평가액']:>14,.0f}원")
    print(f"  현금   {r['현금']:>14,.0f}원")
    print(f"  총자본 {r['총자본']:>14,.0f}원  → 종목당 배정액 "
          f"{r['종목당배정액']:,.0f}원")
    if r["사라진종목"]:
        print(f"\n  목록에 없어 상태에서 뺀 종목 {len(r['사라진종목'])}개: "
              f"{', '.join(r['사라진종목'][:10])}"
              f"{' …' if len(r['사라진종목']) > 10 else ''}")
    if r["새로들어온종목"]:
        print(f"  상태에 없던 보유 {len(r['새로들어온종목'])}개를 등분에 넣었습니다: "
              f"{', '.join(r['새로들어온종목'][:10])}"
              f"{' …' if len(r['새로들어온종목']) > 10 else ''}")
    print(f"\n  등분별 금액: {r['등분별금액']}")
    print("\n  이제 평소대로 `python run_weekly.py`를 돌리세요.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "resync":
        if len(sys.argv) > 2 and sys.argv[2] != "-":
            src, rest = sys.argv[2], sys.argv[3:]
        else:
            src, rest = sys.stdin.read(), sys.argv[3:]
            if not src.strip():
                print("사용법: python run_weekly.py resync 보유종목.csv [현금] [등분수]")
                sys.exit(2)
        _cash = float(rest[0]) if rest else 0.0
        _tr = int(rest[1]) if len(rest) > 1 else 4
        sys.exit(resync_main(src, _cash, _tr))
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        # 인자로 경로를 주거나, 표준입력으로 붙여넣은 내용을 흘려보내도 됩니다.
        if len(sys.argv) > 2 and sys.argv[2] != "-":
            src, rest = sys.argv[2], sys.argv[3:]
        else:
            src, rest = sys.stdin.read(), sys.argv[3:]
            if not src.strip():
                print("사용법: python run_weekly.py seed 보유종목.csv [등분수]\n"
                      "      또는 표준입력으로: ... | python run_weekly.py seed - 4")
                sys.exit(2)
        sys.exit(seed_main(src, int(rest[0]) if rest else 4))
    tr = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    sys.exit(main(tr))
