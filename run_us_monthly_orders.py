# -*- coding: utf-8 -*-
"""미국장 월간 주문서 — 한국 주간 주문서(run_weekly.py)의 미국판.

쓰는 법 (프로필: main=상수 / alt=배우자)

    python run_us_monthly_orders.py                       # 주문서 만들기
    python run_us_monthly_orders.py -p alt                # 배우자 계좌
    python run_us_monthly_orders.py capital 28000         # 약정액 설정 (USD, 한 번)
    python run_us_monthly_orders.py seed "<보유목록>"      # 기존 보유를 등분에 배분
    python run_us_monthly_orders.py resync "<보유목록>" [예수금]
    python run_us_monthly_orders.py fills "<체결내역>" [기준일]
    python run_us_monthly_orders.py reset                 # 미체결 계좌만

**금액은 전부 달러입니다.** us_capital의 총자본과 kor_capital의 총자본을
더하면 수익률이 통째로 거짓이 됩니다. portfolio.set_market("us")와
store의 테이블 가드가 섞임을 막지만, 사람이 숫자를 섞어 읽는 것은 못
막으니 출력에 항상 $를 붙입니다.

**주기에 대하여.** 월 1회 리밸런싱 + **1등분**이 기본입니다. 한 종목이
한 달에 한 번 점검되고, 이게 한국(주 1회 × 4등분 = 4주마다)과 같은 리듬
입니다. 등분을 늘리면 그만큼 느려집니다 — 4등분이면 4개월에 한 번입니다.
숫자를 인자로 주면 바꿀 수 있지만(`run_us_monthly_orders.py 4`), 그러면
백테스트로 잰 것과 다른 전략을 돌리는 것입니다.
"""
import json
import logging
import sys
from datetime import date

import pandas as pd

import config
import factor_global
import http_util
import portfolio
import store

log = logging.getLogger("quant_agent.us_orders")

MARKET = "us"
CUR = "$"


def _fmt(v) -> str:
    try:
        return f"{CUR}{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _price_asof() -> str:
    """주문서가 어느 날 종가로 계산됐는지 — 수량과 순위가 전부 여기서 나옵니다."""
    try:
        d = store.read_sql("select max(Date) as d from global_price;")
        return f"{pd.Timestamp(d['d'].iloc[0]).date()}"
    except Exception:                                      # noqa: BLE001
        return "알 수 없음"


def _resync_age(cap_info: dict) -> str:
    if not cap_info or not cap_info.get("기준일"):
        return "없음 — resync를 한 번도 안 돌렸습니다"
    d = pd.Timestamp(cap_info["기준일"]).date()
    days = (date.today() - d).days
    tail = "" if days <= 35 else "  ← 한 달 넘게 묵었습니다. 거래 전에 맞추세요"
    return f"{d} ({days}일 전){tail}"


def _prof(name: str | None = None) -> dict:
    portfolio.set_market(MARKET)
    return config.profile(name, market=MARKET)


# ── 주문서 ───────────────────────────────────────────────────
def main(tranches: int = 1, profile: str | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(
            config.LOG_DIR / f"us_orders_{date.today():%Y%m%d}.log",
            encoding="utf-8"), logging.StreamHandler()], force=True)
    http_util.install_log_redaction()

    prof = _prof(profile)
    log.info("미국 모델 실행 — %s(%s) · 교차분할 %s",
             prof["라벨"], prof["key"], prof["split"])
    res = factor_global.run(prof["key"])
    picks = pd.read_parquet(config.OUTPUT_DIR / prof["model_file"])
    if picks.empty:
        raise RuntimeError("모델 결과가 비어 있습니다.")

    hold_path = config.OUTPUT_DIR / prof["hold_file"]
    hold_universe = None
    if hold_path.exists():
        hold_universe = (pd.read_parquet(hold_path)["종목코드"]
                         .astype(str).str.upper().tolist())
        log.info("순위 버퍼 명단 %d종목 사용", len(hold_universe))
    else:
        log.warning("%s이 없어 순위 버퍼 없이 돌립니다 — 백테스트와 규칙이 "
                    "달라집니다", prof["hold_file"])

    cap_info = portfolio.load_capital(prof["capital_table"])
    measured = float(cap_info.get("총자본") or 0.0)
    pledged = float(cap_info.get("약정액") or 0.0)
    capital = float(cap_info.get("운용기준") or 0.0)
    cash = float(cap_info.get("현금") or 0.0)
    if capital <= 0:
        raise RuntimeError(
            "미국 계좌의 약정액이 없습니다. 먼저 한 번 설정하세요:\n"
            f"    python run_us_monthly_orders.py -p {prof['key']} "
            f"capital <달러금액>\n"
            "한국의 QUANT_CAPITAL_KRW로 넘어가지 않습니다 — 원화가 달러 "
            "계좌에 들어가는 사고를 막기 위해 일부러 막아 둡니다.")
    log.info("운용기준 $%.2f (약정 $%.2f / 평가 $%.2f + 예수금 $%.2f = $%.2f)",
             capital, pledged, float(cap_info.get("평가액") or 0), cash, measured)

    r = portfolio.rebalance(picks, n=config.N_PORTFOLIO, tranches=tranches,
                            capital=capital, cash=cash,
                            hold_universe=hold_universe,
                            table=prof["state_table"],
                            slot_offset=prof["slot_offset"],
                            weighting=prof["weighting"],
                            max_weight=config.MAX_WEIGHT)

    n_logged = portfolio.log_trades(r["orders"], account=prof["key"],
                                    table=prof["trades_table"])
    log.info("주문 원장 기록 %d건 (%s)", n_logged, prof["key"])

    today = f"{date.today():%Y%m%d}"
    name = (f"{config.REPORT_NAME} 미국 월간주문 "
            f"{'' if prof['key'] == 'main' else prof['라벨'] + ' '}"
            f"usorder-{prof['key']}-{today[4:]}_{config.REPORT_SUFFIX}")
    xlsx = config.OUTPUT_DIR / f"{name}.xlsx"

    held_now = max(0, int(r["보유종목수"]) + int(r["매도"]) - int(r["매수"]))
    rng = r.get("배정액범위")
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        meta = pd.DataFrame([
            {"항목": "기준일", "값": f"{date.today():%Y-%m-%d}"},
            {"항목": "시장 / 통화", "값": "미국 / USD (모든 금액이 달러입니다)"},
            {"항목": "계좌", "값": f"{prof['라벨']} ({prof['key']})"},
            {"항목": "설정",
             "값": (f"점수가중 · 섹터중립 {prof['sector_neutral']} · "
                   f"교차분할 {res.get('교차분할')}")},
            {"항목": "주가 기준일", "값": _price_asof()},
            {"항목": "마지막 재동기화", "값": _resync_age(cap_info)},
            {"항목": "약정 운용금액",
             "값": (_fmt(pledged)
                   + (f"   ({cap_info['램프']})" if cap_info.get("램프")
                      else ""))},
            {"항목": "계좌 실측 (평가액+예수금)", "값": _fmt(measured)},
            {"항목": "배정 기준",
             "값": (_fmt(capital)
                   + ("  (약정액)" if pledged >= measured and pledged > 0
                      else "  (계좌 실측 — 약정액을 넘었습니다)"))},
            {"항목": "종목당 배정액", "값": _fmt(r.get("종목당배정액"))},
            {"항목": "종목별 배정액 범위",
             "값": (f"{_fmt(rng[0])} ~ {_fmt(rng[1])}" if rng else "-")},
            {"항목": "이번에 손보는 등분",
             "값": (("첫 실행(분할 진입) — "
                    f"{r['이번등분']}번 등분부터 달마다 한 등분씩 "
                    f"{tranches}개월에 걸쳐 채웁니다"
                    if r["첫실행"] and r["이번등분"] is not None
                    else "첫 실행 — 전 등분을 한 번에 채움")
                   if r["첫실행"]
                   else f"{r['이번등분']}번 / 총 {tranches}등분")},
            # 검증 여부를 함부로 주장하지 않습니다. 등분 수를 바꾸면
            # 주기가 바뀌고, 그러면 백테스트가 잰 것과 다른 전략입니다.
            {"항목": "한 종목의 점검 주기",
             "값": (f"{tranches}개월에 한 번 "
                   f"(월 1회 리밸런싱 × {tranches}등분)"
                   + ("  ← 한국(4주)과 같은 리듬" if tranches == 1 else
                      f"  ← 한국은 4주마다입니다. {tranches}등분이면 "
                      f"{tranches}배 느립니다"))},
            {"항목": "목표 종목 수", "값": r["목표종목수"]},
            {"항목": "체결 전 실제 보유 (지금 계좌)", "값": held_now},
            {"항목": "주문 뒤 보유 예정", "값": r["보유종목수"]},
            {"항목": "배정됐지만 아직 안 산 종목",
             "값": (f"{r['미매수']}종목 — 다음 등분 차례에 삽니다"
                   if r.get("미매수") else "없음")},
            {"항목": "매도 (전량)", "값": r["매도"]},
            {"항목": "비중조절 (일부만)", "값": r.get("비중조절", 0)},
            {"항목": "현금투입",
             "값": (f"{r.get('현금투입', 0)}종목 / "
                   f"{_fmt(r.get('현금투입금액', 0))}" if r.get("현금투입")
                   else "없음")},
            {"항목": "매수", "값": r["매수"]},
            {"항목": "유지 (손 안 댐)", "값": r["유지"] - r.get("비중조절", 0)},
            # 미국에서 가장 자주 어긋나는 지점입니다. 주가가 $6~$780으로
            # 흩어져 있어서 자본이 작으면 비싼 종목이 통째로 빠지고, 그러면
            # 돌리는 것이 '검증한 전략'이 아니라 '싼 주식만 담은 다른
            # 전략'이 됩니다. 몇 종목이 빠졌는지 매달 여기서 보세요.
            {"항목": "주가가 배정액보다 비싸 제외한 종목",
             "값": (f"{r['가격초과제외']}종목 — 1주도 못 사서 다음 순위로 "
                   f"채웠습니다. 10종목을 넘으면 약정액이 모자란 것입니다"
                   if r.get("가격초과제외") else "없음")},
            {"항목": "비중축소로 확보되는 금액",
             "값": (f"{_fmt(r['확보금액_하한'])} 이상 (전량매도 대금은 별도)"
                   if r.get("확보금액_하한") else "-")},
            {"항목": "매수에 필요한 금액", "값": _fmt(r.get("필요금액"))},
            {"항목": "안내", "값": "아래 '주문' 시트의 종목만 거래하세요. "
                                 "나머지 등분은 이번 달에 손대지 않습니다."},
            {"항목": "구분 읽는 법",
             "값": "매도=전량 비우기 · 비중축소/비중확대=적힌 수량만큼만 "
                  "(종목은 계속 보유) · 현금투입=쌓인 현금으로 추가 매수 "
                  "· 매수=신규 편입"},
            {"항목": "소수점 매매", "값": "쓰지 않습니다 — 수량은 전부 정수입니다"},
            {"항목": "돈이 모자라면",
             "값": "매도·비중축소를 먼저 체결하고, 들어온 현금으로 살 수 있는 "
                  "만큼만 위에서부터 사세요. 못 산 종목은 다음 차례에 채웁니다."},
            {"항목": "환전", "값": "주문 전에 달러가 충분한지 확인하세요. "
                                 "환전이 늦으면 그 달 주문이 통째로 미체결이 "
                                 "되고, 다음 달 주문서가 '이미 샀다'고 "
                                 "계산합니다 — 그때는 resync를 돌리세요."},
        ])
        meta.to_excel(xw, sheet_name="요약", index=False)
        (r["orders"] if len(r["orders"])
         else pd.DataFrame([{"구분": "없음", "종목코드": "",
                             "종목명": "이번 달 거래 없음"}])
         ).to_excel(xw, sheet_name="주문", index=False)
        r["holdings"].to_excel(xw, sheet_name="보유현황", index=False)

    suffix = "" if prof["key"] == "main" else f"_{prof['key']}"
    (config.OUTPUT_DIR / f"us_orders{suffix}_latest.json").write_text(
        json.dumps({
            "시장": "us", "통화": "USD",
            "프로필": prof["key"], "라벨": prof["라벨"],
            "기준일": f"{date.today():%Y-%m-%d}", "이번등분": r["이번등분"],
            "등분수": tranches, "매도": r["매도"], "매수": r["매수"],
            "유지": r["유지"], "보유종목수": r["보유종목수"],
            "체결전_실제보유": held_now, "주문후_보유예정": r["보유종목수"],
            "첫실행": r["첫실행"], "미매수": r.get("미매수", 0),
            "배정완료종목수": r.get("배정완료종목수"),
            "주가기준일": _price_asof(), "재동기화": _resync_age(cap_info),
            "약정액": pledged, "계좌실측": measured, "배정기준": capital,
            "종목당배정액": r.get("종목당배정액"),
            "가격초과제외": r.get("가격초과제외"),
            "교차분할": res.get("교차분할"),
            "orders": r["orders"].to_dict(orient="records"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"  미국장 월간 주문서 · {prof['라벨']} · {date.today():%Y-%m-%d}  (USD)")
    print("=" * 60)
    if r["첫실행"] and r["이번등분"] is not None:
        print(f"  첫 실행(분할 진입) — {tranches}등분 중 {r['이번등분']}번 등분 "
              f"{r['매수']}종목만 담습니다. 나머지 {r.get('미매수', 0)}종목은 "
              f"다음 달부터.")
    elif r["첫실행"]:
        print(f"  첫 실행입니다 — {r['매수']}종목을 한 번에 담습니다.")
    else:
        print(f"  {tranches}등분 중 {r['이번등분']}번 등분 차례")
        print(f"  매도 {r['매도']} · 매수 {r['매수']} · 유지 {r['유지']}")
    print(f"  종목당 배정액 {_fmt(r.get('종목당배정액'))}"
          + (f" · 주가가 비싸 제외 {r['가격초과제외']}종목"
             if r.get("가격초과제외") else ""))
    if int(r.get("가격초과제외") or 0) > 10:
        print(f"  [확인] 제외가 {r['가격초과제외']}종목입니다 — 약정액 "
              f"{_fmt(capital)}으로는 모델 명단을 다 담지 못합니다. "
              f"돌아가는 것은 검증한 전략과 다른 포트폴리오입니다.")
    if len(r["orders"]):
        print()
        for _, o in r["orders"].iterrows():
            qty = o.get("수량")
            qty_s = (f"{int(qty):>5,}주" if isinstance(qty, (int, float)) and qty
                     else f"{str(qty or ''):>6}")
            print(f"   {o['구분']}  {str(o['종목코드']):<6}"
                  f" {str(o['종목명'])[:22]:<22} {qty_s}"
                  f"  {_fmt(o.get('주가')):>10}  {str(o.get('섹터'))[:14]}")
    else:
        print("\n  이번 달은 거래할 것이 없습니다.")
    print(f"\n  엑셀: {xlsx.name}")
    return 0


# ── 보조 명령 ────────────────────────────────────────────────
def capital_main(total: float, cash: float = 0.0,
                 prof: dict | None = None, ramp: bool = False) -> int:
    """이 계좌에 쓰기로 한 **약정 운용금액(달러)** 을 적어 둡니다.

    ramp=True 면 목표액까지 portfolio.RAMP_MONTHS(기본 3개월)에 걸쳐
    매달 같은 폭으로 올립니다. 기존 보유를 모델 포트폴리오로 바꾸는 전환은
    백테스트에 없는 일회성 사건이라, 하루에 몰면 그날 운을 통째로 떠안습니다.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or _prof()
    r = portfolio.set_capital(total, cash=cash, table=prof["capital_table"],
                              ramp=ramp, state_table=prof["state_table"])
    print("\n" + "=" * 60)
    print(f"  약정 운용금액 설정 — {prof['라벨']} 계좌 (미국 · USD)")
    print("=" * 60)
    print(f"  약정액   {_fmt(r['약정액']):>14}  ← 쓰기로 한 돈")
    print(f"  계좌실측 {_fmt(r['총자본']):>14}  "
          f"(평가액 {_fmt(r['평가액'])} + 예수금 {_fmt(r['현금'])})")
    print(f"  배정기준 {_fmt(r['운용기준']):>14}  → 종목당 "
          f"{_fmt(r['종목당배정액'])}")
    if r.get("목표액"):
        print(f"\n  목표 {_fmt(r['목표액'])}까지 {portfolio.RAMP_MONTHS}개월에 "
              f"걸쳐 올립니다 — {r['램프']}")
        print("  달마다 주문서를 돌리기만 하면 배정액이 저절로 올라갑니다 "
              "(다시 설정할 필요 없습니다).")
    alloc = float(r.get("종목당배정액") or 0)
    if alloc > 0:
        print(f"\n  참고: 유동성 통과 미국 종목의 주가 중앙값은 약 $40, "
              f"90분위는 약 $229입니다.")
        if alloc < 150:
            print(f"  종목당 {_fmt(alloc)}이면 후보의 10%% 넘게 1주도 못 삽니다 "
                  f"— 검증한 전략과 다른 포트폴리오가 됩니다.")
    return 0


def seed_main(src: str, tranches: int = 4, prof: dict | None = None) -> int:
    """이미 가지고 있는 미국 주식을 등분에 배분합니다. 시작 전에 한 번만."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or _prof()
    parsed = portfolio.parse_holdings(src)
    print(f"\n  읽어낸 보유 종목 {len(parsed)}개 · "
          f"합계 {_fmt(parsed['평가금액'].sum())}")
    print("  (앞 3개 / 뒤 3개로 제대로 읽혔는지 확인하세요 — 티커가 깨졌으면 "
          "여기서 보입니다)")
    for _, h in pd.concat([parsed.head(3), parsed.tail(3)]).iterrows():
        print(f"    {str(h['종목코드']):<6} {_fmt(h['평가금액']):>12}  "
              f"{h['종목명']}")
    r = portfolio.seed(parsed, tranches=tranches, table=prof["state_table"])
    print("\n" + "=" * 60)
    print(f"  보유 종목을 등분에 배분했습니다 — {prof['라벨']} 계좌 (미국)")
    print("=" * 60)
    print(f"  보유 {r['보유종목수']}종목 · 총 {_fmt(r['총평가액'])}")
    for j, (amt, sh) in enumerate(zip(r["등분별금액"], r["등분별비중"])):
        print(f"   {j}번 등분  {_fmt(amt):>13}  ({sh:.1%})")
    if r["금액균형경고"]:
        print(f"\n  [확인] {r['금액균형경고']}")
    print("\n  이제 달마다 `python run_us_monthly_orders.py`를 돌리면 "
          "한 등분씩 전환합니다.")
    return 0


def resync_main(src: str, cash: float = 0.0, tranches: int = 4,
                prof: dict | None = None) -> int:
    """매달 — 실제 보유 평가액(달러)으로 기준금액과 총자본을 다시 맞춥니다."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or _prof()
    r = portfolio.resync(src, cash=cash, tranches=tranches,
                         table=prof["state_table"],
                         capital_table=prof["capital_table"])
    print("\n" + "=" * 60)
    print(f"  재동기화 완료 — {prof['라벨']} 계좌 (미국 · USD)")
    print("=" * 60)
    print(f"  보유 {r['보유종목수']}종목")
    print(f"  평가액   {_fmt(r['평가액']):>14}")
    print(f"  예수금   {_fmt(r['현금']):>14}")
    print(f"  총자본   {_fmt(r['총자본']):>14}  (계좌 실측)")
    print(f"  약정액   {_fmt(r.get('약정액') or 0):>14}")
    print(f"  배정기준 {_fmt(r.get('운용기준') or r['총자본']):>14}"
          f"  → 종목당 {_fmt(r['종목당배정액'])}")
    if r["사라진종목"]:
        print(f"\n  목록에 없어 상태에서 뺀 종목 {len(r['사라진종목'])}개: "
              f"{', '.join(r['사라진종목'][:10])}"
              f"{' …' if len(r['사라진종목']) > 10 else ''}")
    if r["새로들어온종목"]:
        print(f"  상태에 없던 보유 {len(r['새로들어온종목'])}개를 등분에 "
              f"넣었습니다: {', '.join(r['새로들어온종목'][:10])}"
              f"{' …' if len(r['새로들어온종목']) > 10 else ''}")
    if r.get("미매수유지"):
        print(f"  분할 진입 중 미매수 {r['미매수유지']}종목은 그대로 둡니다")
    return 0


def fills_main(src: str, asof: str | None = None,
               prof: dict | None = None) -> int:
    """증권사 체결내역을 원장에 채웁니다."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or _prof()
    _asof = None
    if asof and str(asof).strip() and str(asof).strip() != "-":
        _asof = pd.Timestamp(str(asof).strip()).date()
    r = portfolio.record_fills(src, account=prof["key"], asof=_asof,
                               table=prof["trades_table"])
    print("\n" + "=" * 60)
    print(f"  체결내역 반영 — {prof['라벨']} 계좌 (미국) · {r['기준일']} 주문")
    print("=" * 60)
    print(f"  체결 반영 {r['체결반영']}건")
    if r["원장밖"]:
        print(f"  주문서에 없던 체결 {r['원장밖']}건 — '원장밖 체결'로 적었습니다")
    if r["미체결"]:
        print(f"  아직 체결 안 된 주문 {r['미체결']}건: "
              f"{', '.join(r['미체결종목'][:10])}"
              f"{' …' if len(r['미체결종목']) > 10 else ''}")
        print("  (환전이 늦어 못 샀다면 다음 달 차례에 다시 후보로 올라옵니다)")
    print("\n  이제 대시보드의 수익률이 계획가가 아니라 실제 체결가로 계산됩니다.")
    return 0


def reset_main(prof: dict | None = None) -> int:
    """아직 체결하지 않은 계좌의 보유 상태를 비웁니다."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    prof = prof or _prof()
    n = portfolio.reset_state(prof["state_table"])
    print("\n" + "=" * 60)
    print(f"  보유 상태 초기화 — {prof['라벨']} 계좌 (미국)")
    print("=" * 60)
    print(f"  {n}종목 → 0종목. 다음 주문서가 '첫 실행'으로 나옵니다.")
    print("  (이미 체결한 계좌라면 이걸 쓰지 말고 resync를 쓰세요)")
    return 0


def _pop_profile(argv: list) -> tuple[str, list]:
    prof, rest, i = None, [], 0
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
    _p = _prof(_profile)
    _cmd = _argv[0] if _argv else ""

    def _f(i: int, d: float = 0.0) -> float:
        try:
            return float(str(_argv[i]).replace(",", "").replace("$", ""))
        except (IndexError, ValueError):
            return d

    if _cmd == "capital":
        _ramp = any(str(a).lower() in ("ramp", "단계", "--ramp")
                    for a in _argv[1:])
        _rest = [a for a in _argv[1:]
                 if str(a).lower() not in ("ramp", "단계", "--ramp")]

        def _g(i, d=0.0):
            try:
                return float(str(_rest[i]).replace(",", "").replace("$", ""))
            except (IndexError, ValueError):
                return d
        sys.exit(capital_main(_g(0), _g(1, 0.0), prof=_p, ramp=_ramp))
    if _cmd == "seed":
        sys.exit(seed_main(_argv[1], prof=_p))
    if _cmd == "resync":
        sys.exit(resync_main(_argv[1], cash=_f(2, 0.0), prof=_p))
    if _cmd == "fills":
        sys.exit(fills_main(_argv[1],
                            _argv[2] if len(_argv) > 2 else None, prof=_p))
    if _cmd == "reset":
        sys.exit(reset_main(prof=_p))
    if _cmd and not _cmd.isdigit():
        print(__doc__)
        sys.exit(2)
    sys.exit(main(int(_argv[0]) if _argv else 4, profile=_profile))
