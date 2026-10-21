# -*- coding: utf-8 -*-
"""inbox 폴더의 CSV를 읽어 체결·보유를 반영합니다.

    python tools/inbox_apply.py <inbox폴더> [처리완료폴더]

왜 폴더인가: Actions 입력창은 한 줄짜리라 100종목을 붙여넣기 어렵고, 네 칸을
따로 채워야 합니다. 증권사에서 받은 CSV를 폴더에 넣어 두면 그걸 읽는 게
훨씬 단순합니다 — 브라우저에서 드래그앤드롭으로 올리면 끝입니다.

파일 이름으로 무엇인지 알아냅니다 (대소문자 무관)
  fills_main_*.csv        상수님 한국 계좌 체결내역
  fills_alt_*.csv         배우자 한국 계좌 체결내역
  holdings_main_*.csv     상수님 한국 계좌 보유목록 (+ 예수금 줄이 있으면 현금)
  holdings_alt_*.csv      배우자 한국 계좌 보유목록
  fills_us_main_*.csv     상수님 **미국** 계좌 체결내역
  holdings_us_alt_*.csv   배우자 **미국** 계좌 보유목록   ... 등

이름에 `us`가 들어가면 미국(USD · us_* 테이블), 없으면 한국(KRW · kor_*)
입니다. 통화가 섞이면 달러 평가액이 원화 총자본에 더해져 수익률이 통째로
거짓이 되므로, 시장은 파일 이름 하나로만 정해지고 추측하지 않습니다.
이름에 YYYY-MM-DD가 있으면 체결내역을 그 날짜 주문에 붙입니다. 없으면 그
계좌의 가장 최근 주문서에 붙입니다.

안에 들어갈 칸 (헤더가 있으면 순서는 상관없습니다)
  체결내역: 종목코드 또는 종목명 · 매매구분(매수/매도) · 체결수량 · 체결단가
  보유목록: 종목코드 · 평가금액 · 종목명   (+ '예수금'/'현금' 줄은 현금으로)

순서: 같은 계좌에서 체결내역을 먼저, 보유목록을 나중에 적용합니다. 보유목록이
실제와 맞추는 최종 기준이기 때문입니다.

한 파일이 실패해도 나머지는 처리합니다. 실패한 파일은 처리완료 폴더로 옮기지
않으므로, 고쳐서 다시 올리면 됩니다.
"""
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import portfolio  # noqa: E402

_DATE = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
KINDS = ("fills", "holdings")


def _parse_name(name: str):
    """파일 이름에서 (종류, 계좌, 기준일)을 알아냅니다."""
    low = name.lower()
    kind = next((k for k in KINDS if low.startswith(k)), None)
    if kind is None:
        return None, None, None, "kr"
    stem = low.replace(".csv", "").replace(".tsv", "").replace(".txt", "")
    # 시장 — 이름에 'us' 토큰이 있으면 미국. 'main'/'alt'와 같은 방식으로
    # 구분자에 둘러싸인 경우만 봅니다 ('plus'나 'august'에 걸리지 않게).
    market = "us" if re.search(r"[_\-.]us([_\-.]|$)", stem) else "kr"
    acct = None
    for key in config.PROFILES:
        if re.search(rf"[_\-.]{key}([_\-.]|$)", stem):
            acct = key
            break
    m = _DATE.search(low)
    asof = None
    if m:
        try:
            asof = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            asof = None
    return kind, acct, asof, market


def _apply(path: Path, kind: str, acct: str, asof, tranches: int,
           market: str = "kr") -> str:
    # **시장 스위치가 먼저입니다.** portfolio는 종목코드를 한국식(6자리 0채움)
    # 으로 다루는 자리가 여러 곳이라, 이걸 안 켜면 'AAPL'이 '00AAPL'이 되거나
    # 통째로 사라집니다. 테이블 가드도 여기서 같이 걸립니다.
    portfolio.set_market(market)
    prof = config.profile(acct, market=market)
    cur = "$" if market == "us" else "원"

    def _m(v) -> str:
        return (f"${float(v):,.2f}" if market == "us"
                else f"{float(v):,.0f}원")

    # 미국은 월 1회 전체 리밸런싱(1등분)이라 한국의 4등분과 다릅니다.
    tr = 1 if market == "us" else tranches

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if kind == "fills":
        r = portfolio.record_fills(text, account=acct, asof=asof,
                                   table=prof["trades_table"])
        msg = (f"체결 {r['체결반영']}건 반영 ({r['기준일']} 주문)"
               + (f" · 원장밖 {r['원장밖']}건" if r["원장밖"] else "")
               + (f" · 미체결 {r['미체결']}건" if r["미체결"] else ""))
        return msg
    rest, cash = portfolio.extract_cash(text)

    # 보유 상태가 비어 있으면 resync가 "먼저 seed를 돌리세요"로 멈춥니다.
    # 계좌를 처음 올리는 날이 바로 그 경우인데, 거기서 막히면 별도 명령을
    # 찾아 돌려야 합니다. 비어 있을 때만 seed로 넘깁니다 — seed는 보유를
    # 등분에 나눠 넣는 '출발점'이라 지금 상황에 정확히 맞는 동작입니다.
    # (상태가 있으면 절대 seed하지 않습니다. 그러면 등분 배정이 날아갑니다.)
    first = portfolio.load_state(prof["state_table"]).empty
    if first:
        sd = portfolio.seed(rest, tranches=tr, table=prof["state_table"])
        msg = (f"**첫 등록(seed)** — 보유 {sd['보유종목수']}종목 · "
               f"총 {_m(sd['총평가액'])} · {tr}등분에 배분")
        if sd.get("금액균형경고"):
            msg += f" · [확인] {sd['금액균형경고']}"
        if cash:
            msg += f" · 예수금 {_m(cash)}은 다음 재동기화에 반영됩니다"
        return msg

    r = portfolio.resync(rest, cash=cash, tranches=tr,
                         table=prof["state_table"],
                         capital_table=prof["capital_table"])
    msg = (f"보유 {r['보유종목수']}종목 · 현금 {_m(cash)} · "
           f"총자본 {_m(r['총자본'])} → 배정액 {_m(r['종목당배정액'])}")
    if r.get("미매수유지"):
        msg += f" · 미매수 {r['미매수유지']}종목 유지"
    if r["사라진종목"]:
        msg += f" · 목록에 없어 뺀 종목 {len(r['사라진종목'])}개"
    if r["새로들어온종목"]:
        msg += f" · 새로 들어온 종목 {len(r['새로들어온종목'])}개"
    return msg


def main(inbox: str, archive: str | None = None,
         tranches: int = 4) -> int:
    box = Path(inbox)
    if not box.is_dir():
        print(f"inbox 폴더가 없습니다: {box}")
        return 0                      # 넣을 게 없는 주도 정상입니다

    files = sorted(p for p in box.iterdir()
                   if p.is_file() and p.suffix.lower() in (".csv", ".txt", ".tsv"))
    if not files:
        print(f"inbox에 처리할 파일이 없습니다 ({box}).")
        return 0

    # 체결 → 보유 순서. 계좌 순서는 파일 이름 순서를 따릅니다.
    plan, skipped = [], []
    for p in files:
        kind, acct, asof, market = _parse_name(p.name)
        if kind is None or acct is None:
            skipped.append((p, "이름에서 종류·계좌를 못 읽음 "
                               "(fills_main_*.csv / holdings_us_alt_*.csv 형식)"))
            continue
        plan.append((KINDS.index(kind), p, kind, acct, asof, market))
    plan.sort(key=lambda x: (x[0], x[1].name))

    print("=" * 60)
    print(f"  inbox 처리 — {len(plan)}개 파일 ({box})")
    print("=" * 60)

    done, failed = [], []
    for _, p, kind, acct, asof, market in plan:
        label = (f"{p.name} [{kind} · {'미국' if market == 'us' else '한국'}"
                 f" · {acct}" + (f" · {asof}" if asof else "") + "]")
        try:
            msg = _apply(p, kind, acct, asof, tranches, market)
            print(f"  [OK] {label}\n       {msg}")
            done.append(p)
        except Exception as e:
            print(f"  [실패] {label}\n       {type(e).__name__}: {str(e)[:200]}")
            failed.append((p, f"{type(e).__name__}: {str(e)[:120]}"))

    for p, why in skipped:
        print(f"  [건너뜀] {p.name} — {why}")

    if archive and done:
        dest = Path(archive) / f"{date.today():%Y-%m-%d}"
        dest.mkdir(parents=True, exist_ok=True)
        for p in done:
            target = dest / p.name
            if target.exists():
                target = dest / f"{p.stem}_{os.getpid()}{p.suffix}"
            shutil.move(str(p), str(target))
        print(f"\n  처리한 {len(done)}개 파일을 {dest}로 옮겼습니다.")

    if failed or skipped:
        print(f"\n  ::warning::처리 못 한 파일 {len(failed) + len(skipped)}개는 "
              f"inbox에 그대로 있습니다. 고쳐서 다시 올리면 됩니다.")
    if not done and (failed or skipped):
        return 1                      # 하나도 못 했으면 실패로 알립니다
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1],
                  sys.argv[2] if len(sys.argv) > 2 else None,
                  int(sys.argv[3]) if len(sys.argv) > 3 else 4))
