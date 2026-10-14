# -*- coding: utf-8 -*-
"""SEC EDGAR 파서 점검 — 네트워크 없이 (fixture로).

이 파서는 제가 실제 응답을 못 보고 썼습니다. 회사 PC와 이 컨테이너 모두
프록시가 sec.gov를 막아서(blocked-by-allowlist) 수집은 GitHub Actions에서만
돌 수 있습니다. 그래서 SEC 문서로 확인한 구조를 fixture로 만들어 파싱 규칙을
여기서 못 박습니다 — Actions에서 처음 돌릴 때 엉뚱한 값이 들어가지 않게.

확인한 실제 구조 (data.sec.gov companyconcept 응답):
  {"cik":..., "taxonomy":"us-gaap", "tag":..., "units":{"USD":[
     {"start":"2009-12-27","end":"2010-03-27","val":3074000000,
      "accn":"...","fy":2010,"fp":"Q2","form":"10-Q","filed":"2010-04-21"}, ...]}}
  · 기간 공시는 start+end, 순간(재무상태표) 공시는 end만
  · 10-Q는 3개월, 10-K는 12개월
"""
import os
import sys
import tempfile

os.environ.setdefault("QUANT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from tools import sec_facts as sf  # noqa: E402

FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def dur(start, end, val, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed,
            "form": form, "accn": "x", "fy": 2025, "fp": "Q1"}


def inst(end, val, filed):
    return {"end": end, "val": val, "filed": filed, "form": "10-Q",
            "accn": "x", "fy": 2025, "fp": "Q1"}


def facts(**tags):
    return {"cik": 1, "entityName": "T",
            "facts": {"us-gaap": {k: {"units": {"USD": v}}
                                  for k, v in tags.items()}}}


print("\n[3개월 공시만 분기로 인정]")
f = facts(NetIncomeLoss=[
    dur("2025-01-01", "2025-03-31", 100, "2025-04-20"),
    dur("2025-04-01", "2025-06-30", 110, "2025-07-20"),
    dur("2025-01-01", "2025-06-30", 210, "2025-07-20"),   # 6개월 누적 — 제외
])
got = sf.extract(f)
ni = got.get("NetIncome", [])
check(len(ni) == 2 and {v for _, v, _ in ni} == {100, 110},
      f"6개월 누적 공시를 분기로 세지 않음 (분기 {len(ni)}개)")

print("\n[같은 기간끝에 3개월·누적이 함께 올 때 — 실제 버그]")
# 10-Q 하나에는 같은 기간끝에 대해 3개월·6개월·9개월 누적이 함께 들어 있고
# **공시일이 모두 같습니다**. 기간으로 거르기 전에 기간끝으로 중복을 없애면
# 누적분이 남았다가 3개월 필터에서 버려져 그 분기가 통째로 사라집니다.
# 실측 피해: AAPL의 분기가 '12월말 1년에 하나'가 되어 2025년 이전 TTM이 0.
f_cum = facts(NetIncomeLoss=[
    dur("2025-01-01", "2025-03-31", 100, "2025-04-20"),          # Q1 3개월
    dur("2025-04-01", "2025-06-30", 110, "2025-07-20"),          # Q2 3개월
    dur("2025-01-01", "2025-06-30", 210, "2025-07-20"),          # 6개월 누적
    dur("2025-07-01", "2025-09-30", 120, "2025-10-20"),          # Q3 3개월
    dur("2025-01-01", "2025-09-30", 330, "2025-10-20"),          # 9개월 누적
])
ni = sf.extract(f_cum).get("NetIncome", [])
vals = sorted(v for _, v, _ in ni)
check(len(ni) == 3 and vals == [100, 110, 120],
      f"누적이 섞여도 3개월 셋을 모두 건짐 ({len(ni)}개 {vals})")
_ends = sorted(f"{d:%m-%d}" for d, _, _ in ni)
check(_ends == ["03-31", "06-30", "09-30"],
      f"분기 세 개가 다 남음 {_ends}")

print("\n[누적만 있고 3개월이 없으면 그 분기는 없다]")
f_only_cum = facts(NetIncomeLoss=[
    dur("2025-01-01", "2025-06-30", 210, "2025-07-20")])
check("NetIncome" not in sf.extract(f_only_cum),
      "6개월 누적만 있으면 분기로 만들지 않음")

print("\n[연간에서 Q4 역산]")
f = facts(NetIncomeLoss=[
    dur("2025-01-01", "2025-03-31", 100, "2025-04-20"),
    dur("2025-04-01", "2025-06-30", 110, "2025-07-20"),
    dur("2025-07-01", "2025-09-30", 120, "2025-10-20"),
    dur("2025-01-01", "2025-12-31", 500, "2026-02-20", "10-K"),  # Q4 미공시
])
ni = sf.extract(f)["NetIncome"]
q4 = [v for d, v, _ in ni if f"{d:%m-%d}" == "12-31"]
check(len(ni) == 4 and q4 and abs(q4[0] - 170) < 1e-9,
      f"Q4 = 연간 500 - (100+110+120) = {q4[0] if q4 else None} (기대 170)")

print("\n[Q4를 따로 공시하면 역산하지 않음]")
f2 = facts(NetIncomeLoss=[
    dur("2025-01-01", "2025-03-31", 100, "2025-04-20"),
    dur("2025-04-01", "2025-06-30", 110, "2025-07-20"),
    dur("2025-07-01", "2025-09-30", 120, "2025-10-20"),
    dur("2025-10-01", "2025-12-31", 180, "2026-02-20"),
    dur("2025-01-01", "2025-12-31", 510, "2026-02-20", "10-K"),
])
ni2 = sf.extract(f2)["NetIncome"]
q4b = [v for d, v, _ in ni2 if f"{d:%m-%d}" == "12-31"]
check(len(ni2) == 4 and abs(q4b[0] - 180) < 1e-9,
      f"공시된 Q4 180을 그대로 씀 (역산값 190이 아니라 {q4b[0]:.0f})")

print("\n[정정 공시는 '처음 보고된 값'을 씀]")
f3 = facts(NetIncomeLoss=[
    dur("2025-01-01", "2025-03-31", 100, "2025-04-20"),
    dur("2025-01-01", "2025-03-31", 95, "2026-04-20"),      # 1년 뒤 정정
])
v = sf.extract(f3)["NetIncome"][0][1]
fl = sf.extract(f3)["NetIncome"][0][2]
check(abs(v - 100) < 1e-9 and f"{fl:%Y-%m-%d}" == "2025-04-20",
      f"처음 보고값 100 / 공시일 {fl:%Y-%m-%d} (정정 95를 쓰면 사후확신)")

print("\n[순간 계정은 end만 있는 공시]")
f4 = facts(Assets=[inst("2025-03-31", 1000, "2025-04-20"),
                   inst("2025-06-30", 1100, "2025-07-20"),
                   dur("2025-01-01", "2025-03-31", 999, "2025-04-20")])
ta = sf.extract(f4)["TotalAssets"]
check(len(ta) == 2 and {v for _, v, _ in ta} == {1000, 1100},
      f"기간 공시가 섞여도 순간 값만 ({len(ta)}개)")

print("\n[태그 후보 — 회사마다 다른 태그]")
f5 = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
    dur("2025-01-01", "2025-03-31", 700, "2025-04-20")])
check("TotalRevenue" in sf.extract(f5),
      "Revenues가 없으면 RevenueFromContractWithCustomer... 를 씀")
f6 = facts(StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest=[
    inst("2025-03-31", 50, "2025-04-20")])
check("StockholdersEquity" in sf.extract(f6),
      "자본도 대체 태그를 씀")

print("\n[매출총이익이 없으면 매출 - 매출원가]")
f7 = facts(
    Revenues=[dur("2025-01-01", "2025-03-31", 700, "2025-04-20"),
              dur("2025-04-01", "2025-06-30", 800, "2025-07-20")],
    CostOfRevenue=[dur("2025-01-01", "2025-03-31", 400, "2025-04-20"),
                   dur("2025-04-01", "2025-06-30", 450, "2025-07-20")])
gp = sf.extract(f7).get("GrossProfit", [])
check(len(gp) == 2 and abs(gp[0][1] - 300) < 1e-9 and abs(gp[1][1] - 350) < 1e-9,
      f"GrossProfit 태그가 없으면 700-400=300, 800-450=350 ({[round(v) for _, v, _ in gp]})")
f8 = facts(GrossProfit=[dur("2025-01-01", "2025-03-31", 111, "2025-04-20")],
           Revenues=[dur("2025-01-01", "2025-03-31", 700, "2025-04-20")],
           CostOfRevenue=[dur("2025-01-01", "2025-03-31", 400, "2025-04-20")])
check(abs(sf.extract(f8)["GrossProfit"][0][1] - 111) < 1e-9,
      "GrossProfit 태그가 있으면 그걸 씀 (역산하지 않음)")

print("\n[USD가 아닌 단위는 무시]")
f9 = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {
    "EUR": [dur("2025-01-01", "2025-03-31", 100, "2025-04-20")],
    "shares": [dur("2025-01-01", "2025-03-31", 5, "2025-04-20")]}}}}}
check("NetIncome" not in sf.extract(f9), "EUR·shares 단위는 안 씀")

print("\n[빈 입력·깨진 입력]")
check(sf.extract({}) == {}, "빈 JSON은 빈 결과")
check(sf.extract({"facts": {}}) == {}, "facts가 비어도 빈 결과")
check(sf.extract(facts(NetIncomeLoss=[
    {"end": "2025-03-31", "val": None, "filed": "2025-04-20"}])) == {},
      "값이 null인 공시는 버림")

print("\n[미래 날짜 공시는 버린다]")
# probe 실측: StockholdersEquity 의 기간말이 2026-12까지 나왔습니다
# (실행일 2026-10-14). 아직 오지 않은 분기로 과거를 테스트하면 안 됩니다.
import pandas as _pd
_fut = (_pd.Timestamp.today().normalize() + _pd.Timedelta(days=120)).strftime("%Y-%m-%d")
_past = (_pd.Timestamp.today().normalize() - _pd.Timedelta(days=30)).strftime("%Y-%m-%d")
f_fut = facts(Assets=[inst(_fut, 999, _past), inst(_past, 100, _past)])
ta = sf.extract(f_fut).get("TotalAssets", [])
check(len(ta) == 1 and abs(ta[0][1] - 100) < 1e-9,
      f"미래 기간말({_fut})을 버리고 과거 값만 ({len(ta)}개)")
f_ff = facts(Assets=[inst(_past, 100, _fut)])
check("TotalAssets" not in sf.extract(f_ff),
      "공시일이 미래인 것도 버림")

print("\n[금융업 매출 태그]")
f_bank = facts(RevenuesNetOfInterestExpense=[
    dur("2025-01-01", "2025-03-31", 500, "2025-04-20")])
check("TotalRevenue" in sf.extract(f_bank),
      "RevenuesNetOfInterestExpense 를 매출로 인정 (은행·보험)")
f_int = facts(InterestAndDividendIncomeOperating=[
    dur("2025-01-01", "2025-03-31", 300, "2025-04-20")])
check("TotalRevenue" in sf.extract(f_int),
      "InterestAndDividendIncomeOperating 도 인정")

print("\n[User-Agent 가드 — 403으로 날리기 전에 멈추는가]")
# 2026-10-14 런이 이메일 없는 UA로 403을 맞고 죽었습니다. 요청을 보내기
# 전에 멈춰야 Actions 분과 시간을 안 버립니다.
import os as _os
_saved = _os.environ.get("QUANT_SEC_UA")
for val, label, should_pass in (
        (None, "설정 안 함", False),
        ("", "빈 값", False),
        ("quant_cloud research script (github.com/x/y)", "이메일 없음", False),
        ("SangsooKim quant research you@example.com", "이름+이메일", True),
        # 2026-10-14 실제 사고: 설정 안내를 두 줄 통째로 붙여넣음.
        # HTTP 헤더에 줄바꿈이 못 들어가서 requests가 거부했습니다.
        ("Name:  QUANT_SEC_UA\nValue: SangsooKim quant research you@example.com",
         "안내문 통째로 붙여넣기", True),
        ("Name: QUANT_SEC_UA\r\nValue: Kim you@example.com", "CRLF 포함", True),
        ("Name: QUANT_SEC_UA\nValue: 이메일없음", "안내문 + 이메일 없음", False)):
    if val is None:
        _os.environ.pop("QUANT_SEC_UA", None)
    else:
        _os.environ["QUANT_SEC_UA"] = val
    try:
        sf._ua()
        ok = True
    except RuntimeError:
        ok = False
    check(ok == should_pass,
          f"{label} → {'통과' if ok else '차단'} (기대 {'통과' if should_pass else '차단'})")
if _saved is None:
    _os.environ.pop("QUANT_SEC_UA", None)
else:
    _os.environ["QUANT_SEC_UA"] = _saved
# 안내문에 변수 이름과 이메일 요구가 들어 있어야 합니다
_pasted = "Name:  QUANT_SEC_UA\r\nValue: SangsooKim quant research you@example.com"
_got = sf._clean_ua(_pasted)
check(_got == "SangsooKim quant research you@example.com",
      f"붙여넣기에서 값만 뽑아냄 → {_got!r}")
check("\n" not in _got and "\r" not in _got,
      "줄바꿈이 남지 않음 (남으면 requests가 헤더를 거부합니다)")
_os.environ["QUANT_SEC_UA"] = _pasted
check(sf._headers()["User-Agent"] == _got, "헤더에 다듬은 값이 들어감")
_os.environ.pop("QUANT_SEC_UA", None)
check("QUANT_SEC_UA" in sf.UA_HELP and "이메일" in sf.UA_HELP,
      "오류 메시지가 무엇을 어디에 넣어야 하는지 알려줌")

print("\n[저장 행 모양]")
rows = sf._rows("AAPL", {"NetIncome": [(pd.Timestamp("2025-03-31"), 100,
                                        pd.Timestamp("2025-04-20"))]})
r = rows[0]
check(set(r) == {"Symbol", "date", "account", "value", "freq", "filed"}
      and r["freq"] == "q",
      f"global_fs 스키마와 같은 칸 ({sorted(r)})")
import store  # noqa: E402
check(set(rows[0]) <= set(store.SCHEMAS["global_fs"]),
      "store의 global_fs 스키마에 모두 들어감")

print()
if FAIL:
    print(f"실패 {FAIL}건 — Actions에서 돌리기 전에 고쳐야 합니다.")
    sys.exit(1)
print("전체 통과 — 처음 보고값을 쓰고, Q4를 역산하고, 단위·태그를 가립니다.")
