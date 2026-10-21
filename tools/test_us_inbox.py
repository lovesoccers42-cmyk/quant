# -*- coding: utf-8 -*-
"""미국 inbox 한 바퀴 — 실제 보유 목록 모양으로.

여기서 막으려는 사고
  1. 미국 CSV가 한국 테이블로 들어가는 것. 달러 평가액이 원화 총자본에
     더해지면 수익률이 통째로 거짓이 되고, 되돌릴 방법이 없습니다.
  2. 티커가 '00AAPL'이 되거나 사라지는 것 (portfolio의 6자리 0채움).
  3. 파일 이름의 'us'를 엉뚱한 단어에서 찾는 것 — 'august', 'plus',
     'bonus'가 미국으로 읽히면 한국 체결이 달러 테이블로 갑니다.

데이터는 2026-10-16 실제 보유 목록의 모양을 그대로 씁니다 (우선주·점이
없는 티커·증권사 '보유수량' 칸 이름 포함).
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QUANT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util as _u  # noqa: E402

import pandas as pd  # noqa: E402

import portfolio as pf  # noqa: E402
import store  # noqa: E402

_spec = _u.spec_from_file_location(
    "inbox_apply", Path(__file__).with_name("inbox_apply.py"))
ia = _u.module_from_spec(_spec)
_spec.loader.exec_module(ia)

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


BOX = Path(tempfile.mkdtemp())
ARCH = Path(tempfile.mkdtemp())

# 증권사 내보내기 모양 그대로 — 헤더가 '보유수량'이고 순서도 다릅니다
US_MAIN = """종목코드,종목명,평가금액,보유수량
APA,APA,294.9,6
BKTI,BK 테크놀로지스,1950.3,30
NUS,뉴스킨 엔터프라이즈,570.54,111
M,메이시스,480,20
CRCT,크리컷,1012.65,157
INVA,이노비바,103.9,5
WHLRP,휠러 리얼 에스테이트 인베스트 우선주,103.95,9
FBIOP,포트리스 바이오테크 우선주 A,66.68,4
"""
US_ALT = """종목코드,종목명,평가금액,보유수량
DLX,딜럭스,139.08,6
NUS,뉴스킨 엔터프라이즈,462.6,90
CRCT,크리컷,625.65,97
HOVNP,HOVNANAN PF A,65.44,4
"""
# 같은 날 한국 계좌도 같이 올라옵니다 — 섞이면 안 됩니다
KR_MAIN = """종목코드,평가금액,종목명,수량
005930,1200000,삼성전자,15
000660,900000,SK하이닉스,3
예수금,350000,,
"""

(BOX / "holdings_us_main_2026-10-16.csv").write_text(US_MAIN, encoding="utf-8")
(BOX / "holdings_us_alt_2026-10-16.csv").write_text(US_ALT, encoding="utf-8")
(BOX / "holdings_main_2026-10-17.csv").write_text(KR_MAIN, encoding="utf-8")

print("[inbox 처리]")
rc = ia.main(str(BOX), str(ARCH), tranches=4)
check(rc == 0, f"세 파일 모두 처리 (반환 {rc})")

print("\n[미국이 미국 테이블로]")
pf.set_market("us")
us_m = pf.load_state("us_portfolio")
us_a = pf.load_state("us_portfolio_alt")
check(len(us_m) == 8, f"상수 미국 8종목 ({len(us_m)})")
check(len(us_a) == 4, f"배우자 미국 4종목 ({len(us_a)})")
check(set(us_m["종목코드"]) == {"APA", "BKTI", "NUS", "M", "CRCT", "INVA",
                              "WHLRP", "FBIOP"},
      f"티커 그대로 — {sorted(us_m['종목코드'])}")
check(not any(str(c).startswith("00") for c in us_m["종목코드"]),
      "'00APA' 같은 코드가 없음")
check("WHLRP" in set(us_m["종목코드"]) and "FBIOP" in set(us_m["종목코드"]),
      "우선주도 일단 상태에 들어감 (정리는 모델 명단이 판단)")

# 첫 등록은 seed라 자본 기록이 아직 없습니다 — 다음 올릴 때 resync가 씁니다.
check(abs(float(pd.to_numeric(us_m["기준금액"]).sum()) - 4582.92) < 0.01,
      f"상수 미국 기준금액 합 ${float(pd.to_numeric(us_m['기준금액']).sum()):,.2f} "
      f"(기대 $4,582.92)")
check(sorted(set(pd.to_numeric(us_m["등분"]))) == [0],
      f"1등분이라 전부 0번 등분 ({sorted(set(pd.to_numeric(us_m['등분'])))})")

print("\n[한국이 한국 테이블로]")
pf.set_market("kr")
kr_m = pf.load_state("kor_portfolio")
check(set(kr_m["종목코드"]) == {"005930", "000660"},
      f"6자리 코드 유지 — {sorted(kr_m['종목코드'])}")
kr_sum = float(pd.to_numeric(kr_m["기준금액"]).sum())
check(abs(kr_sum - 2_100_000) < 1, f"한국 기준금액 합 {kr_sum:,.0f}원")

print("\n[두 번째 올리기 — 이번엔 seed가 아니라 resync]")
# 첫 파일은 seed(첫 등록)라 기록 테이블을 건드리지 않습니다. 실제로
# 달마다 일어나는 일은 resync이고, 아래 버그는 거기서 터집니다.
(BOX / "holdings_us_main_2026-11-20.csv").write_text(US_MAIN, encoding="utf-8")
(BOX / "holdings_main_2026-11-20.csv").write_text(KR_MAIN, encoding="utf-8")
rc2 = ia.main(str(BOX), str(ARCH), tranches=4)
check(rc2 == 0, f"두 번째 처리 (반환 {rc2})")

print("\n[재동기화 기록도 시장별로 — 2026-10-21에 잡힌 버그]")
# resync가 종목별 평가금액을 적는 테이블이 모듈 상수 하나(kor_holdings_log)
# 였습니다. 그러면 미국 재동기화가 **달러 평가액을 한국 기록에** 씁니다.
# 이 테이블이 대시보드 평가액 곡선과 시간가중수익률의 원천이라, 섞이는
# 순간 한국 성과가 통째로 망가지고 되돌릴 수 없습니다.
us_log = store.read("us_holdings_log")
kr_log = store.read("kor_holdings_log")
check(not us_log.empty, f"미국 기록이 us_holdings_log에 ({len(us_log)}행)")
us_codes = set(us_log["종목코드"].astype(str))
kr_codes = set(kr_log["종목코드"].astype(str)) if not kr_log.empty else set()
check(us_codes <= {"APA", "BKTI", "NUS", "M", "CRCT", "INVA", "WHLRP",
                   "FBIOP", "DLX", "HOVNP"},
      f"미국 기록은 티커뿐 ({sorted(us_codes)})")
check(not (us_codes & kr_codes),
      "한국 기록에 미국 티커가 섞이지 않음")
check(kr_codes <= {"005930", "000660"},
      f"한국 기록은 6자리 코드뿐 ({sorted(kr_codes)})")
us_amt = float(pd.to_numeric(us_log["평가금액"]).max())
check(us_amt < 1e5, f"한국 기록에 달러 자릿수가 안 들어감 (미국 최대 {us_amt:,.0f})")

print("\n[통화가 섞이지 않았는가 — 가장 중요]")
us_sum = float(pd.to_numeric(us_m["기준금액"]).sum())
check(kr_sum > 1e6 and us_sum < 1e5,
      f"원화 {kr_sum:,.0f} / 달러 {us_sum:,.2f} — 자릿수가 섞이지 않음")
check(set(kr_m["종목코드"]).isdisjoint(set(us_m["종목코드"])),
      "두 시장의 보유가 겹치지 않음")

print("\n[처리 후 정리]")
left = sorted(p.name for p in BOX.iterdir())
check(not left, f"inbox가 비었음 ({left})")
moved = sorted(p.name for p in ARCH.rglob("*.csv"))
check(len(moved) == 5, f"archive로 5개 이동 ({len(moved)})")

print("\n[이름에 us가 없으면 한국 — 오탐 방지]")
for nm, want in (("holdings_august_main.csv", "kr"),
                 ("fills_plus_alt.csv", "kr"),
                 ("holdings_bonus_main.csv", "kr"),
                 ("holdings_us_main.csv", "us"),
                 ("holdings-us-alt.csv", "us")):
    got = ia._parse_name(nm)[3]
    check(got == want, f"{nm:32s} → {got} (기대 {want})")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 고치기 전에는 미국 CSV를 inbox에 올리지 마세요.")
    sys.exit(1)
print("전체 통과 — 달러와 원화가 서로 다른 테이블에 들어갑니다.")
