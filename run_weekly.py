# -*- coding: utf-8 -*-
"""한국장 주간 분할 리밸런싱 — 이번 주 주문서를 만듭니다.

    python run_weekly.py                    # 이번 주 주문서
    python run_weekly.py 4                  # 등분 수를 직접 지정
    python run_weekly.py seed 보유종목.csv   # 처음 한 번 — 보유 종목을 등분에 배분
    python run_weekly.py resync 보유종목.csv 350000   # 매주 — 평가액·현금 갱신
    python run_weekly.py capital 10000000 -p alt    # 현금으로 시작할 때 한 번
    python run_weekly.py reset -p alt               # 체결 전 상태 되돌리기
    python run_weekly.py fills 체결내역.csv           # 거래 후 — 실제 체결가 기록

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


def _price_asof() -> str:
    """모델이 쓴 주가의 마지막 날짜. 주문서가 언제 종가로 계산됐는지입니다."""
    try:
        df = store.read("kor_price")
        if df.empty:
            return "없음"
        return f"{pd.to_datetime(df['날짜']).max():%Y-%m-%d}"
    except Exception:
        return "확인 실패"


def _resync_age(cap_info: dict) -> str:
    """마지막 재동기화가 며칠 전인지. 오래됐으면 경고 문구까지."""
    if not cap_info or not cap_info.get("기준일"):
        return ("없음 — 상태가 실제 보유와 다를 수 있습니다. "
                "매도 주문을 믿지 마시고 먼저 재동기화하세요")
    d = pd.Timestamp(cap_info["기준일"]).date()
    days = (date.today() - d).days
    txt = f"{d:%Y-%m-%d} ({days}일 전)"
    if days >= 10:
        txt += " — 오래됐습니다. 보유 목록으로 재동기화하기 전에는 매도 주문을 믿지 마세요"
    return txt


def _setting_text(prof: dict) -> str:
    cap = ("없음" if prof["max_sector_pct"] >= 1
           else f"{prof['max_sector_pct']:.0%}")
    return (f"비중 {prof['weighting']} · 섹터중립 {prof['sector_neutral']}"
            f" · 섹터상한 {cap}")


def main(tranches: int = 4, profile: str | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"weekly_{date.today():%Y%m%d}.log", encoding="utf-8"),
            logging.StreamHandler()], force=True)
    http_util.install_log_redaction()

    prof = config.profile(profile)
    log.info("모델 실행 — %s(%s) 순위 갱신 · 비중 %s · 섹터중립 %s · 섹터상한 %s",
             prof["라벨"], prof["key"], prof["weighting"],
             prof["sector_neutral"], prof["max_sector_pct"])
    res = factor_kor.run(prof["key"])
    picks = pd.read_parquet(config.OUTPUT_DIR / prof["model_file"])
    if picks.empty:
        raise RuntimeError("모델 결과가 비어 있습니다.")

    hold_path = config.OUTPUT_DIR / prof["hold_file"]
    hold_universe = None
    if hold_path.exists():
        hold_universe = (pd.read_parquet(hold_path)["종목코드"]
                         .astype(str).str.zfill(6).tolist())
        log.info("순위 버퍼 명단 %d종목 사용", len(hold_universe))
    else:
        log.warning("%s이 없어 순위 버퍼 없이 돌립니다 " % prof["hold_file"] +
                    "— 백테스트와 규칙이 달라집니다")

    # 배정액의 기준은 **운용기준** = max(약정액, 평가액 + 예수금)입니다.
    #
    # 왜 평가액+예수금이 아닌가 (실측): 배우자 계좌는 1,000만원을 쓰기로 했지만
    # 살 때 그때그때 입금하므로 예수금이 0이고, 분할 진입 2주차 평가액은
    # 210만원이었습니다. 평가액+예수금으로 계산하면 종목당 배정액이
    # 10만원에서 2.1만원으로 쪼그라들어 분할 진입이 스스로 멈춥니다.
    # 약정액을 기준으로 두면 분할 진입이 계획대로 끝나고, 나중에 평가액이
    # 약정액을 넘어서면 그때부터 평가액이 기준이 됩니다(복리 반영).
    cap_info = portfolio.load_capital(prof["capital_table"])
    measured = float(cap_info.get("총자본") or 0.0)
    pledged = float(cap_info.get("약정액") or 0.0)
    capital = float(cap_info.get("운용기준") or 0.0) or config.CAPITAL_KRW
    cash = float(cap_info.get("현금") or 0.0)
    if cap_info:
        log.info("운용기준 %.0f원 (약정액 %.0f / 평가액 %.0f + 예수금 %.0f = %.0f) · %s",
                 capital, pledged, cap_info.get("평가액", 0), cash, measured,
                 pd.Timestamp(cap_info["기준일"]).date())
        if pledged <= 0:
            log.warning("약정액이 없습니다 — 평가액+예수금(%.0f)으로 배정액을 "
                        "계산합니다. 예수금을 미리 넣지 않는 방식이면 배정액이 "
                        "줄어듭니다. `run_weekly.py -p %s capital <금액>`을 "
                        "한 번 돌리세요.", measured, prof["key"])
    else:
        log.warning("자금 기록이 없습니다 — QUANT_CAPITAL_KRW(%.0f)를 씁니다. "
                    "`run_weekly.py capital <금액>`을 한 번 돌리세요.",
                    config.CAPITAL_KRW)

    r = portfolio.rebalance(picks, n=config.N_PORTFOLIO, tranches=tranches,
                            capital=capital, cash=cash,
                            hold_universe=hold_universe,
                            table=prof["state_table"],
                            slot_offset=prof["slot_offset"],
                            weighting=prof["weighting"],
                            max_weight=config.MAX_WEIGHT)

    # 주문을 원장에 쌓습니다 — 이게 없으면 그 주 거래 기록이 영구히
    # 사라집니다(상태 테이블은 보유만 담고, 주문서 파일은 매주 덮어써짐).
    n_logged = portfolio.log_trades(r["orders"], account=prof["key"])
    log.info("주문 원장 기록 %d건 (%s)", n_logged, prof["key"])

    today = f"{date.today():%Y%m%d}"
    # 파일명에 weekly-main / weekly-alt 토큰을 넣습니다 — GitHub 릴리스가
    # 한글을 떼어내므로 토큰이 없으면 백테스트 리포트와 이름이 겹쳐 덮입니다.
    name = (f"{config.REPORT_NAME} 한국 주간주문 "
            f"{'' if prof['key'] == 'main' else prof['라벨'] + ' '}"
            f"weekly-{prof['key']}-{today[4:]}_{config.REPORT_SUFFIX}")
    xlsx = config.OUTPUT_DIR / f"{name}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        meta = pd.DataFrame([
            {"항목": "기준일", "값": f"{date.today():%Y-%m-%d}"},
            {"항목": "계좌", "값": f"{prof['라벨']} ({prof['key']})"},
            {"항목": "설정", "값": _setting_text(prof)},
            # 주문서가 어느 날 종가로 계산됐는지 — 수량과 순위가 모두 이
            # 날짜의 주가에서 나옵니다. 기준일보다 이틀 이상 뒤처져 있으면
            # 주가 수집이 실패한 것이니 거래 전에 확인하세요.
            {"항목": "주가 기준일", "값": _price_asof()},
            # 상태는 '주문서를 뽑을 때 샀다고 가정한' 값입니다. 실제 체결과
            # 벌어지면 가지고 있지도 않은 종목을 팔라는 주문이 나옵니다
            # (실측: 연습으로 돌린 주문서의 매수 종목이 보유로 남아 그 다음
            # 주 매도 주문에 등장). 재동기화가 그걸 바로잡는 유일한 수단이라
            # 얼마나 오래됐는지 여기 적습니다.
            {"항목": "마지막 재동기화", "값": _resync_age(cap_info)},
            # 배정액이 어느 금액에서 나왔는지 — 이걸 적지 않으면 예수금을
            # 안 넣은 주에 배정액이 왜 작아졌는지 알 수 없습니다.
            {"항목": "약정 운용금액",
             "값": (f"{pledged:,.0f}원" if pledged > 0
                   else "미설정 — capital 명령으로 한 번 설정하세요")},
            {"항목": "계좌 실측 (평가액+예수금)", "값": f"{measured:,.0f}원"},
            {"항목": "배정 기준",
             "값": (f"{capital:,.0f}원 "
                   + ("(약정액)" if pledged >= measured and pledged > 0
                      else "(계좌 실측 — 약정액을 넘었습니다)"))},
            {"항목": "종목별 배정액 범위",
             "값": (f"{r['배정액범위'][0]:,}원 ~ {r['배정액범위'][1]:,}원"
                   if r.get("배정액범위") else "-")},
            {"항목": "이번에 손보는 등분",
             "값": (("첫 실행(분할 진입) — "
                    f"{r['이번등분']}번 등분부터 매주 한 등분씩 "
                    f"{tranches}주에 걸쳐 채웁니다"
                    if r["첫실행"] and r["이번등분"] is not None
                    else "첫 실행 — 전 등분을 한 번에 채움")
                   if r["첫실행"]
                   else f"{r['이번등분']}번 / 총 {tranches}등분")},
            {"항목": "목표 종목 수", "값": r["목표종목수"]},
            {"항목": "현재 보유", "값": r["보유종목수"]},
            {"항목": "배정됐지만 아직 안 산 종목",
             "값": (f"{r['미매수']}종목 — 다음 등분 차례에 삽니다"
                   if r.get("미매수") else "없음")},
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

    (config.OUTPUT_DIR / (f"weekly_kr{'' if prof['key'] == 'main' else '_' + prof['key']}"
                          f"_latest.json")).write_text(json.dumps({
        "프로필": prof["key"], "라벨": prof["라벨"],
        "기준일": f"{date.today():%Y-%m-%d}", "이번등분": r["이번등분"],
        "등분수": tranches, "매도": r["매도"], "매수": r["매수"], "유지": r["유지"],
        "보유종목수": r["보유종목수"], "첫실행": r["첫실행"],
        "미매수": r.get("미매수", 0), "배정완료종목수": r.get("배정완료종목수"),
        "주가기준일": _price_asof(),
        "재동기화": _resync_age(cap_info),
        "약정액": pledged, "계좌실측": measured, "배정기준": capital,
        "종목당배정액": r.get("종목당배정액"), "가격초과제외": r.get("가격초과제외"),
        "orders": r["orders"].to_dict(orient="records"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  한국장 주간 주문서 · {prof['라벨']} · {date.today():%Y-%m-%d}")
    print("=" * 60)
    if r["첫실행"] and r["이번등분"] is not None:
        print(f"  첫 실행(분할 진입) — {tranches}등분 중 {r['이번등분']}번 등분 "
              f"{r['매수']}종목만 담습니다. 나머지 {r.get('미매수', 0)}종목은 "
              f"다음 차례에.")
    elif r["첫실행"]:
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


def seed_main(src: str, tranches: int = 4, prof: dict | None = None) -> int:
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

    prof = prof or config.profile()
    r = portfolio.seed(parsed, tranches=tranches, table=prof["state_table"])

    print("\n" + "=" * 60)
    print(f"  보유 종목을 등분에 배분했습니다 — {prof['라벨']} 계좌")
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


def resync_main(src: str, cash: float = 0.0, tranches: int = 4,
                prof: dict | None = None) -> int:
    """매주 — 실제 보유 평가액으로 기준금액과 총자본을 다시 맞춥니다."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or config.profile()
    r = portfolio.resync(src, cash=cash, tranches=tranches,
                         table=prof["state_table"],
                         capital_table=prof["capital_table"])
    print("\n" + "=" * 60)
    print(f"  재동기화 완료 — {prof['라벨']} 계좌")
    print("=" * 60)
    print(f"  보유 {r['보유종목수']}종목")
    print(f"  평가액 {r['평가액']:>14,.0f}원")
    print(f"  현금   {r['현금']:>14,.0f}원")
    print(f"  총자본 {r['총자본']:>14,.0f}원  (계좌 실측)")
    print(f"  약정액 {float(r.get('약정액') or 0):>14,.0f}원")
    print(f"  배정기준 {float(r.get('운용기준') or r['총자본']):>12,.0f}원"
          f"  → 종목당 {r['종목당배정액']:,.0f}원")
    if r["사라진종목"]:
        print(f"\n  목록에 없어 상태에서 뺀 종목 {len(r['사라진종목'])}개: "
              f"{', '.join(r['사라진종목'][:10])}"
              f"{' …' if len(r['사라진종목']) > 10 else ''}")
    if r["새로들어온종목"]:
        print(f"  상태에 없던 보유 {len(r['새로들어온종목'])}개를 등분에 넣었습니다: "
              f"{', '.join(r['새로들어온종목'][:10])}"
              f"{' …' if len(r['새로들어온종목']) > 10 else ''}")
    if r.get("미매수유지"):
        print(f"  분할 진입 중 미매수 {r['미매수유지']}종목은 그대로 둡니다 "
              f"(팔린 것이 아니니까요)")
    print(f"\n  등분별 금액: {r['등분별금액']}")
    print("\n  이제 평소대로 `python run_weekly.py`를 돌리세요.")
    return 0


def log_main(src: str, prof: dict | None = None) -> int:
    """주문서 json에서 원장을 되살립니다 (원장이 생기기 전 주문 백필)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or config.profile()
    r = portfolio.log_trades_from_json(src, account=prof["key"])
    print("\n" + "=" * 60)
    print(f"  원장 백필 — {prof['라벨']} 계좌")
    print("=" * 60)
    print(f"  {r['기준일']} 주문 {r['기록']}건을 원장에 넣었습니다.")
    if r.get("프로필확인") and r["프로필확인"] != prof["key"]:
        print(f"  [확인] json의 프로필은 '{r['프로필확인']}'인데 "
              f"'{prof['key']}' 계좌로 넣었습니다 — 계좌를 확인하세요.")
    return 0


def fills_main(src: str, asof: str | None = None,
               prof: dict | None = None) -> int:
    """증권사 체결내역을 원장에 채웁니다 (거래 체결 후 아무 때나)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or config.profile()
    _asof = None
    if asof and str(asof).strip() and str(asof).strip() != "-":
        _asof = pd.Timestamp(str(asof).strip()).date()
    r = portfolio.record_fills(src, account=prof["key"], asof=_asof)
    print("\n" + "=" * 60)
    print(f"  체결내역 반영 — {prof['라벨']} 계좌 · {r['기준일']} 주문")
    print("=" * 60)
    print(f"  체결 반영 {r['체결반영']}건")
    if r["원장밖"]:
        print(f"  주문서에 없던 체결 {r['원장밖']}건 — '원장밖 체결'로 적었습니다")
    if r["미체결"]:
        print(f"  아직 체결 안 된 주문 {r['미체결']}건: "
              f"{', '.join(r['미체결종목'][:10])}"
              f"{' …' if len(r['미체결종목']) > 10 else ''}")
        print("  (못 산 종목은 다음 등분 차례에 다시 후보로 올라옵니다)")
    print("\n  이제 대시보드의 수익률이 계획가가 아니라 실제 체결가로 계산됩니다.")
    return 0


def reset_main(prof: dict | None = None) -> int:
    """아직 체결하지 않은 계좌의 보유 상태를 비웁니다."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or config.profile()
    n = portfolio.reset_state(prof["state_table"])
    print("\n" + "=" * 60)
    print(f"  보유 상태 초기화 — {prof['라벨']} 계좌")
    print("=" * 60)
    print(f"  {n}종목 → 0종목. 다음 주문서가 '첫 실행'으로 나옵니다.")
    print("  (이미 체결한 계좌라면 이걸 쓰지 말고 resync를 쓰세요)")
    return 0


def capital_main(total: float, cash: float = 0.0,
                 prof: dict | None = None) -> int:
    """이 계좌에 쓰기로 한 **약정 운용금액**을 적어 둡니다 (한 번만)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or config.profile()
    r = portfolio.set_capital(total, cash=cash, table=prof["capital_table"])
    print("\n" + "=" * 60)
    print(f"  약정 운용금액 설정 — {prof['라벨']} 계좌")
    print("=" * 60)
    print(f"  약정액   {r['약정액']:>14,.0f}원  ← 쓰기로 한 돈")
    print(f"  계좌실측 {r['총자본']:>14,.0f}원  "
          f"(평가액 {r['평가액']:,.0f} + 예수금 {r['현금']:,.0f})")
    print(f"  배정기준 {r['운용기준']:>14,.0f}원  → 종목당 "
          f"{r['종목당배정액']:,.0f}원")
    print("\n  예수금을 미리 넣지 않아도 됩니다 — 배정액은 약정액에서 나옵니다.")
    print("  평가액이 약정액을 넘어서면 그때부터 평가액이 기준이 됩니다.")
    return 0


def _pop_profile(argv: list) -> tuple[str, list]:
    """--profile alt 또는 -p alt 를 찾아 떼어냅니다."""
    prof = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--profile", "-p") and i + 1 < len(argv):
            prof = argv[i + 1]; i += 2; continue
        if a.startswith("--profile="):
            prof = a.split("=", 1)[1]; i += 1; continue
        rest.append(a); i += 1
    return (prof or config.DEFAULT_PROFILE), rest


if __name__ == "__main__":
    _profile, _argv = _pop_profile(sys.argv[1:])
    _prof = config.profile(_profile)
    sys.argv = [sys.argv[0]] + _argv
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
        sys.exit(resync_main(src, _cash, _tr, _prof))
    if len(sys.argv) > 1 and sys.argv[1] == "log":
        if len(sys.argv) < 3:
            print("사용법: python run_weekly.py log weekly_kr_alt_latest.json "
                  "[-p alt]")
            sys.exit(2)
        sys.exit(log_main(sys.argv[2], _prof))
    if len(sys.argv) > 1 and sys.argv[1] == "fills":
        if len(sys.argv) > 2 and sys.argv[2] != "-":
            src, rest = sys.argv[2], sys.argv[3:]
        else:
            src, rest = sys.stdin.read(), sys.argv[3:]
            if not src.strip():
                print("사용법: python run_weekly.py fills 체결내역.csv [기준일] "
                      "[-p alt]")
                sys.exit(2)
        sys.exit(fills_main(src, rest[0] if rest else None, _prof))
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        sys.exit(reset_main(_prof))
    if len(sys.argv) > 1 and sys.argv[1] == "capital":
        if len(sys.argv) < 3:
            print("사용법: python run_weekly.py capital 10000000 [대기현금] "
                  "[-p alt]")
            sys.exit(2)
        _total = float(str(sys.argv[2]).replace(",", "").strip())
        _cash = float(str(sys.argv[3]).replace(",", "").strip()) \
            if len(sys.argv) > 3 else 0.0
        sys.exit(capital_main(_total, _cash, _prof))
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
        sys.exit(seed_main(src, int(rest[0]) if rest else 4, _prof))
    tr = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    sys.exit(main(tr, _prof["key"]))
