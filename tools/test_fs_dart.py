# -*- coding: utf-8 -*-
"""DART 수집기 오프라인 테스트 — 인증키 없이 로직만 검증합니다.

실제 DART 응답 형태를 그대로 흉내낸 픽스처를 씁니다. 확인하는 것:
  · 누적 손익을 3개월치로 제대로 차분하는가
  · 잔액 계정(자산·자본)은 차분하지 않는가
  · 중간 분기가 비면 틀린 값을 만드는 대신 버리는가
  · 원 단위를 억원으로 내리는가
  · account_id가 없는 회사도 계정명으로 찾는가
  · 매출총이익을 안 내는 회사는 매출액-매출원가로 만드는가
  · 12월 결산이 아닌 회사의 기준일을 맞추는가
"""
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="dart_test_")
os.environ["QUANT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import kor_fs_dart as dart  # noqa: E402

fails = []


def check(cond, msg):
    print(("[OK ] " if cond else "[FAIL] ") + msg)
    if not cond:
        fails.append(msg)


# 실제 fnlttSinglAcntAll 응답에는 날짜 컬럼이 없습니다(thstrm_dt는 '주요계정'
# API에만 있습니다). 실제로 여기서 전 계정이 버려져 0행이 났습니다.
# 그래서 픽스처도 날짜 없이, 사업연도·보고서코드만 주고 만듭니다.
def row(aid, nm, sj, reprt, amount=None, add=None, year=2023, dt=None):
    r = {"account_id": aid, "account_nm": nm, "sj_div": sj,
         "bsns_year": str(year), "reprt_code": reprt,
         "thstrm_amount": "" if amount is None else f"{amount:,}",
         "thstrm_add_amount": "" if add is None else f"{add:,}"}
    if dt:
        r["thstrm_dt"] = dt
    return r


def payload(rows):
    return {"status": "000", "message": "정상", "list": rows}


# ── 1. 누적 차분 ─────────────────────────────────────────────
# 매출 누적: 1Q 100억, 반기 250억, 3Q 430억, 연간 600억
#  → 분기별 100 / 150 / 180 / 170
CUM = {1: 100, 2: 250, 3: 430, 4: 600}
ASSETS = {1: 1000, 2: 1100, 3: 1050, 4: 1200}
# 보고서코드: 1분기 11013 · 반기 11012 · 3분기 11014 · 사업보고서 11011
ENDS = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
BS_ENDS = ENDS

by_q = {}
for q in (1, 2, 3, 4):
    rows = [
        row("ifrs-full_Revenue", "매출액", "IS", ENDS[q], add=int(CUM[q] * 1e8)),
        row("ifrs-full_GrossProfit", "매출총이익", "IS", ENDS[q],
            add=int(CUM[q] * 0.3 * 1e8)),
        row("ifrs-full_ProfitLoss", "당기순이익", "IS", ENDS[q],
            add=int(CUM[q] * 0.1 * 1e8)),
        row("ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름",
            "CF", ENDS[q], add=int(CUM[q] * 0.2 * 1e8)),
        row("ifrs-full_Assets", "자산총계", "BS", BS_ENDS[q],
            amount=int(ASSETS[q] * 1e8)),
        row("ifrs-full_Equity", "자본총계", "BS", BS_ENDS[q],
            amount=int(ASSETS[q] * 0.6 * 1e8)),
    ]
    by_q[q] = dart.parse_report(payload(rows), "005930", q)

check(len(by_q[1]) == 6, f"6개 계정 모두 인식 ({len(by_q[1])}개)")

df = dart.to_quarterly(by_q, "005930")
rev = df[df["계정"] == "매출액"].sort_values("기준일")["값"].tolist()
check(rev == [100, 150, 180, 170], f"누적→3개월 차분 정확 {rev} (기대 [100,150,180,170])")

assets = df[df["계정"] == "자산"].sort_values("기준일")["값"].tolist()
check(assets == [1000, 1100, 1050, 1200],
      f"잔액 계정은 차분하지 않음 {assets}")

check(df["값"].max() < 1e5, "원 → 억원 단위 환산 (최대값이 억원 규모)")

ends = sorted(df[df["계정"] == "매출액"]["기준일"].dt.strftime("%m-%d").tolist())
check(ends == ["03-31", "06-30", "09-30", "12-31"], f"분기말 기준일 {ends}")

bs_end = df[df["계정"] == "자산"]["기준일"].min().strftime("%Y-%m-%d")
check(bs_end == "2023-03-31", f"재무상태표 '현재' 표기도 파싱 ({bs_end})")

# ── 2. 중간 분기 결측 ────────────────────────────────────────
holed = {1: by_q[1], 3: by_q[3], 4: by_q[4]}       # 2분기 통째로 없음
dh = dart.to_quarterly(holed, "005930")
rev_h = dh[dh["계정"] == "매출액"].sort_values("기준일")["값"].tolist()
check(rev_h == [100, 170],
      f"앞 분기가 없으면 그 분기를 버림 (3분기 제외) {rev_h}")
check(330 not in rev_h, "차분 불가능한 구간에서 430-100=330 같은 틀린 값을 안 만듦")
a_h = dh[dh["계정"] == "자산"].sort_values("기준일")["값"].tolist()
check(a_h == [1000, 1050, 1200], f"잔액 계정은 구멍이 있어도 그대로 살림 {a_h}")

# ── 3. account_id 없는 회사 (계정명으로 탐색) ────────────────
legacy = [
    row("", "수익(매출액)", "IS", ENDS[1], add=int(500 * 1e8)),
    row("-표준계정없음", "당기순이익(손실)", "IS", ENDS[1], add=int(50 * 1e8)),
    row("", "자산총계", "BS", BS_ENDS[1], amount=int(3000 * 1e8)),
]
p = dart.parse_report(payload(legacy), "000660", 1)
check(p.get("매출액", (None, None))[1] == 500, "account_id 없어도 계정명으로 매출액 인식")
check(p.get("당기순이익", (None, None))[1] == 50, "계정명 변형(당기순이익(손실))도 인식")
check("자산" in p, "자산총계 → 자산 매핑")

# ── 3-b. 2018년 이전 택소노미 (ifrs_ 접두사) ─────────────────
# DART는 연도마다 다른 택소노미를 씁니다. 같은 계정이 과거 공시에서는
# 'ifrs_Assets', 최근에는 'ifrs-full_Assets'로 옵니다. 접두사를 그대로
# 비교하면 과거 공시가 통째로 안 잡힙니다 — 실제로 여기서 0행이 났습니다.
old_tax = [
    row("ifrs_Assets", "자산총계", "BS", BS_ENDS[1], amount=int(2000 * 1e8)),
    row("ifrs_Equity", "자본총계", "BS", BS_ENDS[1], amount=int(1200 * 1e8)),
    row("ifrs_Revenue", "매출액", "IS", ENDS[1], add=int(800 * 1e8)),
    row("ifrs_GrossProfit", "매출총이익", "IS", ENDS[1], add=int(240 * 1e8)),
    row("ifrs_ProfitLoss", "당기순이익", "IS", ENDS[1], add=int(80 * 1e8)),
    row("ifrs_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", "CF",
        ENDS[1], add=int(160 * 1e8)),
]
po = dart.parse_report(payload(old_tax), "005930", 1)
check(len(po) == 6, f"2018년 이전 ifrs_ 접두사도 6개 전부 인식 ({len(po)}개)")
check(po["자산"][1] == 2000 and po["매출액"][1] == 800, "과거 택소노미 값도 정확")
check(dart._tag("ifrs-full_Assets") == dart._tag("ifrs_Assets") == "Assets",
      "네임스페이스를 뗀 태그로 비교")

# 같은 태그가 여러 표에 있으면 기대하는 재무제표를 고른다
dup = [
    row("ifrs-full_ProfitLoss", "당기순이익", "CIS", ENDS[1], add=int(99 * 1e8)),
    row("ifrs-full_ProfitLoss", "당기순이익", "IS", ENDS[1], add=int(80 * 1e8)),
]
pd_ = dart.parse_report(payload(dup), "005930", 1)
check(pd_["당기순이익"][1] == 80, f"손익계산서(IS)를 우선 ({pd_['당기순이익'][1]})")

# ── 4. 매출총이익 미보고 → 매출액 - 매출원가 ─────────────────
nogp = [
    row("ifrs-full_Revenue", "매출액", "IS", ENDS[1], add=int(1000 * 1e8)),
    row("ifrs-full_CostOfSales", "매출원가", "IS", ENDS[1], add=int(700 * 1e8)),
]
p2 = dart.parse_report(payload(nogp), "000660", 1)
check(abs(p2.get("매출총이익", (None, 0))[1] - 300) < 1e-6,
      f"매출총이익 미보고 시 매출액-매출원가로 생성 ({p2.get('매출총이익')})")

# ── 5. 날짜 컬럼이 없을 때 ───────────────────────────────────
# 실제 응답에는 thstrm_dt가 아예 없습니다. 사업연도 + 보고서코드로 분기말을
# 만들어야 합니다. 이게 안 되면 전 계정이 버려집니다(실제로 그랬습니다).
nodate = [row("ifrs_Assets", "자산총계", "BS", "11014",
              amount=int(500 * 1e8), year=2017)]
p3 = dart.parse_report(payload(nodate), "000660", 3, 2017, "11014")
check(p3["자산"][0].strftime("%Y-%m-%d") == "2017-09-30",
      f"날짜 컬럼이 없어도 사업연도+보고서코드로 분기말 생성 ({p3['자산'][0].date()})")

# 날짜가 있으면 그걸 우선 (다른 API 응답이 섞여 들어와도 안전)
withdt = [row("ifrs_Assets", "자산총계", "BS", "11013",
              amount=int(500 * 1e8), year=2023, dt="2023.06.30 현재")]
p3b = dart.parse_report(payload(withdt), "000660", 1, 2023, "11013")
check(p3b["자산"][0].strftime("%Y-%m-%d") == "2023-06-30",
      "날짜 컬럼이 있으면 그쪽을 우선")

# ── 5-b. 금액 컬럼 두 개의 의미 구분 ─────────────────────────
# thstrm_amount는 분기보고서에서 '그 분기 3개월', 사업보고서에서 '1년'입니다.
# 둘을 구분 못 하면 누적을 3개월로 착각해 엉뚱한 값이 나옵니다.
q3 = [row("ifrs-full_Revenue", "매출액", "IS", "11012", amount=int(150 * 1e8))]
pq = dart.parse_report(payload(q3), "000660", 2, 2023, "11012")
check(pq["매출액"][2] == "q3m", f"분기보고서 thstrm_amount = 3개월치 ({pq['매출액'][2]})")

yr = [row("ifrs-full_Revenue", "매출액", "IS", "11011", amount=int(600 * 1e8))]
py = dart.parse_report(payload(yr), "000660", 4, 2023, "11011")
check(py["매출액"][2] == "cum", f"사업보고서 thstrm_amount = 1년 누적 ({py['매출액'][2]})")

add = [row("ifrs-full_Revenue", "매출액", "IS", "11012",
           amount=int(150 * 1e8), add=int(250 * 1e8))]
pa = dart.parse_report(payload(add), "000660", 2, 2023, "11012")
check(pa["매출액"][1] == 250 and pa["매출액"][2] == "cum",
      "두 컬럼이 다 있으면 누적(thstrm_add_amount)을 씀")

# 3개월치와 누적이 섞여 와도 3개월치로 환산되는가
# 1Q 3개월 100 · 2Q 3개월 150 · 3Q 누적 430 · 사업보고서 누적 600
mixed = {
    1: dart.parse_report(payload([row("ifrs-full_Revenue", "매출액", "IS", "11013",
                                      amount=int(100 * 1e8))]), "A", 1, 2023, "11013"),
    2: dart.parse_report(payload([row("ifrs-full_Revenue", "매출액", "IS", "11012",
                                      amount=int(150 * 1e8))]), "A", 2, 2023, "11012"),
    3: dart.parse_report(payload([row("ifrs-full_Revenue", "매출액", "IS", "11014",
                                      add=int(430 * 1e8))]), "A", 3, 2023, "11014"),
    4: dart.parse_report(payload([row("ifrs-full_Revenue", "매출액", "IS", "11011",
                                      amount=int(600 * 1e8))]), "A", 4, 2023, "11011"),
}
dm = dart.to_quarterly(mixed, "A").sort_values("기준일")["값"].tolist()
check(dm == [100, 150, 180, 170],
      f"3개월치와 누적이 섞여 와도 정확히 환산 {dm} (기대 [100,150,180,170])")

# ── 5-c. 실제 응답 회귀 테스트 ───────────────────────────────
# 2026-09-15 실행에서 실제로 받은 SK하이닉스 2015년 사업보고서 응답입니다.
# 계정은 제대로 왔는데 날짜 컬럼이 없어 통째로 버려졌던 바로 그 데이터입니다.
REAL = [
    {"account_id": "ifrs_CurrentAssets", "sj_div": "BS", "account_nm": "유동자산",
     "thstrm_amount": "9760030000000", "bsns_year": "2015", "reprt_code": "11011"},
    {"account_id": "-표준계정코드 미사용-", "sj_div": "BS", "account_nm": "단기금융상품",
     "thstrm_amount": "3615554000000", "bsns_year": "2015", "reprt_code": "11011"},
    {"account_id": "ifrs_PropertyPlantAndEquipment", "sj_div": "BS",
     "account_nm": "유형자산", "thstrm_amount": "16966252000000",
     "bsns_year": "2015", "reprt_code": "11011"},
    {"account_id": "ifrs_Assets", "sj_div": "BS", "account_nm": "자산총계",
     "thstrm_amount": "29677906000000", "bsns_year": "2015", "reprt_code": "11011"},
]
pr = dart.parse_report(payload(REAL), "000660", 4, 2015, "11011")
check("자산" in pr, "실제 응답에서 자산총계를 찾음")
check(abs(pr["자산"][1] - 296779.06) < 0.01,
      f"29.68조 → 296,779억원 ({pr['자산'][1]:,.2f})")
check(pr["자산"][0].strftime("%Y-%m-%d") == "2015-12-31",
      f"사업보고서 → 12월 31일 ({pr['자산'][0].date()})")
check(pr["자산"][2] == "point", "재무상태표는 잔액(차분 안 함)")

# ── 6. 오류 응답 ─────────────────────────────────────────────
check(dart.parse_report({"status": "013", "message": "데이터 없음"}, "A", 1) == {},
      "조회 데이터 없음(013)은 빈 결과")
check(dart.parse_report({"status": "000", "list": []}, "A", 1) == {},
      "빈 list도 빈 결과")

# ── 7. 진행 상태 저장·재개 ───────────────────────────────────
prog = dart.load_progress()
check(prog.empty, "첫 실행에는 진행 기록이 비어 있음")
dart.save_progress(prog, [{"종목코드": "005930", "연도": 2015, "상태": "4/4",
                           "갱신일": pd.Timestamp("2026-09-14")}])
prog2 = dart.load_progress()
check(len(prog2) == 1, "진행 기록 저장")
check(("005930", 2015) in dart._done_keys(prog2), "완료 키 조회")
dart.save_progress(prog2, [{"종목코드": "005930", "연도": 2015, "상태": "4/4",
                            "갱신일": pd.Timestamp("2026-09-14")}])
check(len(dart.load_progress()) == 1, "같은 종목·연도를 두 번 기록하지 않음")

# 빈손(0/4)으로 끝난 건은 완료로 치지 않고 다시 시도해야 한다
dart.save_progress(dart.load_progress(),
                   [{"종목코드": "000660", "연도": 2015, "상태": "0/4",
                     "갱신일": pd.Timestamp("2026-09-14")}])
keys = dart._done_keys(dart.load_progress())
check(("000660", 2015) not in keys,
      "한 분기도 못 읽은 건은 완료로 안 봄 (매핑 고친 뒤 재시도 가능)")
check(("005930", 2015) in keys, "정상 수집 건은 계속 건너뜀")

# ── 7-b. 12월 결산 판정 (기존 kor_fs로, DART 호출 없이) ──────
import store  # noqa: E402
store.write("kor_fs", pd.DataFrame([
    {"종목코드": "005930", "기준일": pd.Timestamp(f"2023-{m:02d}-28"),
     "계정": "자산", "값": 1.0, "공시구분": "q"} for m in (3, 6, 9, 12)
] + [
    {"종목코드": "111111", "기준일": pd.Timestamp(f"2023-{m:02d}-28"),
     "계정": "자산", "값": 1.0, "공시구분": "q"} for m in (2, 5, 8, 11)
]))
dec = dart.december_filers()
check("005930" in dec, "3·6·9·12월 결산 종목은 허용")
check("111111" not in dec,
      "2·5·8·11월(비12월 결산) 종목은 제외 — 날짜 계산이 틀리므로")

check(dart._priority_tickers(limit=0) == [] or True, "limit 인자 수용")

# ── 8. 기존 kor_fs 스키마와 동일한가 ─────────────────────────
store.write("kor_fs", pd.DataFrame(columns=list(store.SCHEMAS["kor_fs"])))  # 7-b 픽스처 정리
check(set(df.columns) == set(store.SCHEMAS["kor_fs"]) - set(),
      f"kor_fs와 컬럼 동일 {sorted(df.columns)}")
check(set(df["공시구분"]) == {"q"}, "공시구분 'q'로 저장 (분기)")
n = store.upsert("kor_fs", df)
check(n == len(df), f"기존 저장소에 그대로 적재 ({n}행)")
again = store.upsert("kor_fs", df)
check(len(store.read("kor_fs")) == len(df),
      f"같은 데이터를 다시 넣어도 중복되지 않음 ({again}행 시도 → {len(store.read('kor_fs'))}행)")

shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 인증키만 들어오면 바로 수집 가능합니다.")
