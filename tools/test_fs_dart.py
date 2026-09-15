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


def row(aid, nm, sj, dt, amount=None, add=None):
    return {"account_id": aid, "account_nm": nm, "sj_div": sj, "thstrm_dt": dt,
            "thstrm_amount": "" if amount is None else f"{amount:,}",
            "thstrm_add_amount": "" if add is None else f"{add:,}"}


def payload(rows):
    return {"status": "000", "message": "정상", "list": rows}


# ── 1. 누적 차분 ─────────────────────────────────────────────
# 매출 누적: 1Q 100억, 반기 250억, 3Q 430억, 연간 600억
#  → 분기별 100 / 150 / 180 / 170
CUM = {1: 100, 2: 250, 3: 430, 4: 600}
ASSETS = {1: 1000, 2: 1100, 3: 1050, 4: 1200}
ENDS = {1: "2023.01.01 ~ 2023.03.31", 2: "2023.01.01 ~ 2023.06.30",
        3: "2023.01.01 ~ 2023.09.30", 4: "2023.01.01 ~ 2023.12.31"}
BS_ENDS = {1: "2023.03.31 현재", 2: "2023.06.30 현재",
           3: "2023.09.30 현재", 4: "2023.12.31 현재"}

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

# ── 4. 매출총이익 미보고 → 매출액 - 매출원가 ─────────────────
nogp = [
    row("ifrs-full_Revenue", "매출액", "IS", ENDS[1], add=int(1000 * 1e8)),
    row("ifrs-full_CostOfSales", "매출원가", "IS", ENDS[1], add=int(700 * 1e8)),
]
p2 = dart.parse_report(payload(nogp), "000660", 1)
check(abs(p2.get("매출총이익", (None, 0))[1] - 300) < 1e-6,
      f"매출총이익 미보고 시 매출액-매출원가로 생성 ({p2.get('매출총이익')})")

# ── 5. 12월 결산이 아닌 회사 ─────────────────────────────────
march = [row("ifrs-full_Revenue", "매출액", "IS",
             "2023.04.01 ~ 2023.06.30", add=int(200 * 1e8))]
p3 = dart.parse_report(payload(march), "000660", 1)
check(p3["매출액"][0].strftime("%Y-%m-%d") == "2023-06-30",
      "reprt_code로 추측하지 않고 실제 기간 종료일을 씀 (3월 결산 대응)")

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

# ── 8. 기존 kor_fs 스키마와 동일한가 ─────────────────────────
import store  # noqa: E402
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
