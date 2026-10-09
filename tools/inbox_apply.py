# -*- coding: utf-8 -*-
"""inbox 폴더의 CSV를 읽어 체결·보유를 반영합니다.

    python tools/inbox_apply.py <inbox폴더> [처리완료폴더]

왜 폴더인가: Actions 입력창은 한 줄짜리라 100종목을 붙여넣기 어렵고, 네 칸을
따로 채워야 합니다. 증권사에서 받은 CSV를 폴더에 넣어 두면 그걸 읽는 게
훨씬 단순합니다 — 브라우저에서 드래그앤드롭으로 올리면 끝입니다.

파일 이름으로 무엇인지 알아냅니다 (대소문자 무관)
  fills_main_*.csv     상수님 계좌 체결내역
  fills_alt_*.csv      배우자 계좌 체결내역
  holdings_main_*.csv  상수님 계좌 보유목록 (+ 예수금 줄이 있으면 현금으로)
  holdings_alt_*.csv   배우자 계좌 보유목록
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
        return None, None, None
    acct = None
    for key in config.PROFILES:
        if re.search(rf"[_\-.]{key}([_\-.]|$)", low.replace(".csv", "")):
            acct = key
            break
    m = _DATE.search(low)
    asof = None
    if m:
        try:
            asof = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            asof = None
    return kind, acct, asof


def _apply(path: Path, kind: str, acct: str, asof, tranches: int) -> str:
    prof = config.profile(acct)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if kind == "fills":
        r = portfolio.record_fills(text, account=acct, asof=asof)
        msg = (f"체결 {r['체결반영']}건 반영 ({r['기준일']} 주문)"
               + (f" · 원장밖 {r['원장밖']}건" if r["원장밖"] else "")
               + (f" · 미체결 {r['미체결']}건" if r["미체결"] else ""))
        return msg
    rest, cash = portfolio.extract_cash(text)
    r = portfolio.resync(rest, cash=cash, tranches=tranches,
                         table=prof["state_table"],
                         capital_table=prof["capital_table"])
    msg = (f"보유 {r['보유종목수']}종목 · 현금 {cash:,.0f}원 · "
           f"총자본 {r['총자본']:,.0f}원 → 배정액 {r['종목당배정액']:,.0f}원")
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
        kind, acct, asof = _parse_name(p.name)
        if kind is None or acct is None:
            skipped.append((p, "이름에서 종류·계좌를 못 읽음 "
                               "(fills_main_*.csv / holdings_alt_*.csv 형식)"))
            continue
        plan.append((KINDS.index(kind), p, kind, acct, asof))
    plan.sort(key=lambda x: (x[0], x[1].name))

    print("=" * 60)
    print(f"  inbox 처리 — {len(plan)}개 파일 ({box})")
    print("=" * 60)

    done, failed = [], []
    for _, p, kind, acct, asof in plan:
        label = f"{p.name} [{kind} · {acct}" + (f" · {asof}" if asof else "") + "]"
        try:
            msg = _apply(p, kind, acct, asof, tranches)
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
