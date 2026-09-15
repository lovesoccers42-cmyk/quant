# -*- coding: utf-8 -*-
"""DART OpenAPI 재무제표 수집 — 과거 이력 확장용.

왜 만드나
---------
백테스트의 t값이 낮은 건 모델이 약해서가 아니라 관측이 적어서입니다.
t = IR × √기간이라, 연 IR 0.76에 2.84년이면 t는 1.28밖에 안 나옵니다.
같은 실력으로 11년을 재면 t ≈ 2.5입니다. 그래서 기간을 늘리는 게
모델을 건드리는 어떤 방법보다 확실합니다.

막고 있던 건 주가가 아니라 재무제표였습니다(FnGuide는 최근 분기만 줍니다).
DART는 2015년부터 전 상장사의 분기 재무제표를 공개합니다.

기존 kor_fs와 완전히 같은 스키마(종목코드·기준일·계정·값·공시구분)로 적재해서
factor_core와 backtest가 코드 수정 없이 그대로 씁니다.

주의한 것들
-----------
1. DART의 손익·현금흐름은 '누적'입니다. 1분기 3개월, 반기 6개월, 3분기 9개월,
   사업보고서 12개월. FnGuide처럼 3개월치로 쓰려면 앞 분기를 빼야 합니다.
   중간 분기가 비면 차분이 틀리므로 그 분기는 아예 버립니다(틀린 값보다 빈 값).
2. 기준일은 reprt_code로 추측하지 않고 응답의 thstrm_dt에서 읽습니다.
   12월 결산이 아닌 회사가 있기 때문입니다.
3. 단위가 원이라 억원으로 내립니다(기존 kor_fs가 억원).
4. 하루 20,000회 호출 제한이 있어 중단·재개가 됩니다.
"""
from __future__ import annotations

import io
import logging
import zipfile
from xml.etree import ElementTree

import pandas as pd

import config
import http_util
import store

log = logging.getLogger("quant_agent.fs_dart")

BASE = "https://opendart.fss.or.kr/api"
DAILY_LIMIT = 20_000          # DART 인증키 1개의 하루 호출 한도
UNIT = 1e8                    # 원 → 억원 (기존 kor_fs 단위)
PROGRESS = "kor_dart_progress"

# (보고서코드, 분기, 누적개월)
REPORTS = [("11013", 1, 3), ("11012", 2, 6), ("11014", 3, 9), ("11011", 4, 12)]

# 기존 kor_fs 계정명 → (IFRS 표준 account_id, 계정명 대체 후보)
# account_id가 먼저입니다. 회사가 계정명을 제멋대로 붙여도 표준 ID는 같습니다.
TARGETS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "자산": (("ifrs-full_Assets",), ("자산총계",)),
    "자본": (("ifrs-full_Equity",), ("자본총계",)),
    "매출액": (("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"),
             ("매출액", "수익(매출액)", "영업수익", "매출")),
    "매출총이익": (("ifrs-full_GrossProfit",), ("매출총이익",)),
    "당기순이익": (("ifrs-full_ProfitLoss",),
               ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익",
                "당기순손익")),
    "영업활동으로인한현금흐름": (
        ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
        ("영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동 현금흐름")),
}

# 잔액 계정 — 시점의 값이라 차분하지 않습니다. 나머지는 전부 흐름 계정.
STOCK_ACCOUNTS = {"자산", "자본"}

# 매출총이익을 따로 안 내는 회사가 많습니다 (매출액 - 매출원가로 만듭니다)
COST_OF_SALES = ("ifrs-full_CostOfSales",)
COST_OF_SALES_NM = ("매출원가",)


def _norm(s) -> str:
    """계정명 비교용 — 공백과 괄호 안 주석을 지웁니다."""
    return "".join(str(s or "").split())


def _amount(row: dict, *, cumulative: bool):
    """흐름 계정은 누적(thstrm_add_amount)을 우선합니다.

    반기보고서의 thstrm_amount를 '4~6월 3개월'로 내는 회사와 '1~6월 누적'으로
    내는 회사가 섞여 있습니다. 누적 컬럼은 정의가 하나뿐이라 안전합니다.
    """
    keys = (["thstrm_add_amount", "thstrm_amount"] if cumulative
            else ["thstrm_amount"])
    for k in keys:
        v = str(row.get(k) or "").replace(",", "").strip()
        if v in ("", "-"):
            continue
        try:
            return float(v)
        except ValueError:
            continue
    return None


def _period_end(row: dict):
    """thstrm_dt에서 기간 종료일. '2023.01.01 ~ 2023.03.31' / '2023.03.31 현재'."""
    import re
    found = re.findall(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", str(row.get("thstrm_dt") or ""))
    if not found:
        return None
    y, m, d = found[-1]
    try:
        return pd.Timestamp(int(y), int(m), int(d))
    except ValueError:
        return None


def _pick(rows: list, ids: tuple, names: tuple):
    """account_id 우선, 없으면 계정명으로 한 행을 고릅니다."""
    wanted_nm = {_norm(n) for n in names}
    by_name = None
    for r in rows:
        rid = str(r.get("account_id") or "").strip()
        if rid in ids:
            return r
        if by_name is None and _norm(r.get("account_nm")) in wanted_nm:
            by_name = r
    return by_name


def parse_report(payload: dict, ticker: str, quarter: int) -> dict:
    """DART 응답 하나 → {계정: (기준일, 값)}. 값은 억원, 흐름은 아직 누적입니다."""
    if str(payload.get("status")) != "000":
        return {}
    rows = payload.get("list") or []
    if not rows:
        return {}

    out: dict[str, tuple] = {}
    for account, (ids, names) in TARGETS.items():
        row = _pick(rows, ids, names)
        if row is None:
            continue
        is_stock = account in STOCK_ACCOUNTS
        val = _amount(row, cumulative=not is_stock)
        end = _period_end(row)
        if val is None or end is None:
            continue
        out[account] = (end, val / UNIT)

    # 매출총이익을 안 내는 회사 — 매출액에서 매출원가를 뺍니다
    if "매출총이익" not in out and "매출액" in out:
        cost = _pick(rows, COST_OF_SALES, COST_OF_SALES_NM)
        if cost is not None:
            cv = _amount(cost, cumulative=True)
            if cv is not None:
                end, rev = out["매출액"]
                out["매출총이익"] = (end, rev - cv / UNIT)
    return out


def to_quarterly(by_quarter: dict, ticker: str) -> pd.DataFrame:
    """{분기: {계정: (기준일, 누적값)}} → 3개월치 kor_fs 행.

    흐름 계정은 직전 분기 누적을 뺍니다. 직전 분기가 없으면 그 분기는 버립니다.
    """
    recs = []
    for q in (1, 2, 3, 4):
        cur = by_quarter.get(q)
        if not cur:
            continue
        prev = by_quarter.get(q - 1) if q > 1 else {}
        for account, (end, val) in cur.items():
            if account in STOCK_ACCOUNTS:
                amount = val
            elif q == 1:
                amount = val
            else:
                if not prev or account not in prev:
                    continue            # 차분할 앞 분기가 없음 → 버림
                amount = val - prev[account][1]
            recs.append({"종목코드": ticker, "기준일": end, "계정": account,
                         "값": float(amount), "공시구분": "q"})
    return pd.DataFrame(recs)


# ── API 호출 ────────────────────────────────────────────────
def corp_map(api_key: str) -> dict[str, str]:
    """{종목코드: corp_code}. 한 번만 받으면 되고 호출 한도에 거의 영향 없습니다."""
    r = http_util.get(f"{BASE}/corpCode.xml", params={"crtfc_key": api_key})
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    root = ElementTree.fromstring(xml)
    out = {}
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if stock and len(stock) == 6 and corp:
            out[stock] = corp
    if not out:
        raise RuntimeError("corpCode.xml에서 상장 종목을 찾지 못했습니다 "
                           "(인증키가 잘못됐거나 응답 형식이 바뀌었습니다)")
    return out


def fetch_report(api_key: str, corp: str, year: int, reprt: str) -> tuple[dict, int]:
    """전체 재무제표 한 건. 연결(CFS)이 없으면 별도(OFS)로 한 번 더.

    (응답, 실제 호출 횟수)를 돌려줍니다 — 호출 한도를 정확히 세야 하기 때문입니다.
    """
    used = 0
    for fs_div in ("CFS", "OFS"):
        http_util.polite_sleep(config.DART_SLEEP)
        used += 1
        r = http_util.get(f"{BASE}/fnlttSinglAcntAll.json", params={
            "crtfc_key": api_key, "corp_code": corp, "bsns_year": str(year),
            "reprt_code": reprt, "fs_div": fs_div})
        try:
            data = r.json()
        except ValueError:
            return {"status": "999", "message": "JSON 아님"}, used
        status = str(data.get("status"))
        if status == "000":
            return data, used
        if status == "020":                      # 사용 한도 초과
            raise RuntimeError("DART 일일 호출 한도를 넘었습니다 (status 020)")
        if status == "010":
            raise RuntimeError("DART 인증키가 등록되지 않았습니다 (status 010)")
        if status == "013":                      # 조회 데이터 없음 → 별도(OFS) 시도
            continue
        return data, used
    return {"status": "013", "message": "조회된 데이터 없음"}, used


# ── 진행 상태 ────────────────────────────────────────────────
def load_progress() -> pd.DataFrame:
    df = store.read(PROGRESS)
    if df.empty:
        return pd.DataFrame(columns=["종목코드", "연도", "상태", "갱신일"])
    return df


def _done_keys(prog: pd.DataFrame) -> set:
    if prog.empty:
        return set()
    return set(zip(prog["종목코드"].astype(str), prog["연도"].astype(int)))


def save_progress(prog: pd.DataFrame, new_rows: list) -> int:
    if new_rows:
        add = pd.DataFrame(new_rows)
        prog = add if prog.empty else pd.concat([prog, add], ignore_index=True)
    prog = prog.drop_duplicates(subset=["종목코드", "연도"], keep="last")
    store.write(PROGRESS, prog)
    return len(prog)


def collect(api_key: str | None = None, years=None, tickers=None,
            budget: int = DAILY_LIMIT, chunk: int = 200) -> dict:
    """유동성 상위 종목부터 연도별로 채웁니다. 한도에 걸리면 거기서 멈춥니다.

    같은 종목·연도를 두 번 받지 않도록 진행 상태를 남기므로,
    다음 날 그대로 다시 돌리면 이어서 받습니다.
    """
    api_key = api_key or config.DART_API_KEY
    if not api_key:
        raise RuntimeError(
            "DART_API_KEY가 없습니다. opendart.fss.or.kr에서 인증키를 발급받아 "
            "GitHub Secrets에 DART_API_KEY로 넣어주세요.")

    years = list(years or range(config.DART_START_YEAR, pd.Timestamp.today().year + 1))
    tickers = list(tickers or _priority_tickers())
    if not tickers:
        raise RuntimeError("수집할 종목이 없습니다 (kor_ticker가 비었습니다).")

    cmap = corp_map(api_key)
    prog = load_progress()
    done = _done_keys(prog)

    calls, buffer, new_prog = 1, [], []
    rows_saved, skipped, missing = 0, 0, 0
    today = pd.Timestamp.today().normalize()
    stopped = None

    for ticker in tickers:
        corp = cmap.get(ticker)
        if corp is None:
            missing += 1
            continue
        for year in years:
            if (ticker, year) in done:
                skipped += 1
                continue
            # 한 종목·연도는 최대 8회(4보고서 × 연결/별도) 씁니다
            if calls + len(REPORTS) * 2 > budget:
                stopped = "호출 한도"
                break

            by_q, got = {}, 0
            try:
                for reprt, q, _months in REPORTS:
                    data, used = fetch_report(api_key, corp, year, reprt)
                    calls += used
                    parsed = parse_report(data, ticker, q)
                    if parsed:
                        by_q[q] = parsed
                        got += 1
            except RuntimeError as e:
                stopped = str(e)
                break

            if by_q:
                df = to_quarterly(by_q, ticker)
                if len(df):
                    buffer.append(df)
            new_prog.append({"종목코드": ticker, "연도": int(year),
                             "상태": f"{got}/4", "갱신일": today})

            if len(buffer) >= chunk:
                rows_saved += store.upsert("kor_fs", pd.concat(buffer, ignore_index=True))
                buffer = []
                save_progress(prog, new_prog)
                prog, new_prog = load_progress(), []
                log.info("DART 수집 %d회 호출 · %d행 누적", calls, rows_saved)
        if stopped:
            break

    if buffer:
        rows_saved += store.upsert("kor_fs", pd.concat(buffer, ignore_index=True))
    total = save_progress(prog, new_prog)

    remaining = len(tickers) * len(years) - total
    return {"호출수": calls, "저장행수": rows_saved, "완료": total,
            "남은작업": max(remaining, 0), "건너뜀": skipped,
            "DART에없음": missing, "중단사유": stopped or "완료"}


def _priority_tickers() -> list[str]:
    """유동성 높은 종목부터. 어차피 유동성 필터에서 걸릴 종목에 호출을 안 씁니다."""
    tickers = store.common_tickers()
    if not store.exists("kor_price"):
        return tickers
    try:
        rank = store.read_sql(f"""
            select 종목코드, avg(종가 * 거래량) 거래대금
            from kor_price
            where 날짜 >= (select max(날짜) - interval {config.LIQUIDITY_WINDOW} day
                         from kor_price)
            group by 종목코드 order by 거래대금 desc nulls last;
        """)
    except Exception:
        return tickers
    order = [t for t in rank["종목코드"].astype(str).str.zfill(6) if t in set(tickers)]
    return order + [t for t in tickers if t not in set(order)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(collect())
