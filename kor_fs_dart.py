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
2. 응답에 날짜 컬럼이 없습니다. 사업연도·보고서코드로 분기말을 만들기 때문에
   12월 결산이 아닌 회사는 아예 건너뜁니다(틀린 날짜는 미래 정보 오염입니다).
3. 단위가 원이라 억원으로 내립니다(기존 kor_fs가 억원).
4. 하루 20,000회 호출 제한이 있어 중단·재개가 됩니다.
5. 응답 한 건이 3~4초라 동시에 여러 건을 받습니다. 순차로는 Actions 작업
   시간(6시간) 안에 하루 한도를 못 씁니다.
"""
from __future__ import annotations

import io
import logging
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
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

# 기존 kor_fs 계정명 → (IFRS 태그 이름, 계정명 대체 후보)
#
# 태그는 네임스페이스를 뗀 뒤 비교합니다. DART가 연도마다 다른 택소노미를 쓰기
# 때문입니다 — 2018년 이전 공시는 'ifrs_Assets', 이후는 'ifrs-full_Assets'로
# 같은 계정에 다른 접두사가 붙습니다. 접두사를 그대로 비교하면 과거 공시가
# 통째로 안 잡힙니다.
TARGETS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "자산": (("Assets",), ("자산총계", "자산총계계", "자산")),
    "자본": (("Equity",), ("자본총계", "자본")),
    "매출액": (("Revenue", "RevenueFromContractsWithCustomers",
             "RevenueFromSaleOfGoods", "RevenueFromRenderingOfServices"),
             ("매출액", "수익(매출액)", "영업수익", "매출", "수익",
              "매출및지분법손익", "영업수익(매출액)")),
    "매출총이익": (("GrossProfit",), ("매출총이익", "매출총이익(손실)")),
    "당기순이익": (("ProfitLoss",),
               ("당기순이익", "당기순이익(손실)", "당기순손익", "분기순이익",
                "반기순이익", "연결당기순이익", "당기순이익(손실)합계")),
    "영업활동으로인한현금흐름": (
        ("CashFlowsFromUsedInOperatingActivities",),
        ("영업활동현금흐름", "영업활동으로인한현금흐름", "영업활동으로인한순현금흐름",
         "영업활동순현금흐름")),
}

# 잔액 계정 — 시점의 값이라 차분하지 않습니다. 나머지는 전부 흐름 계정.
STOCK_ACCOUNTS = {"자산", "자본"}

# 재무제표 구분 — 같은 이름이 여러 표에 나올 때 우선순위를 줍니다.
PREFERRED_SJ = {"자산": ("BS",), "자본": ("BS",),
                "매출액": ("IS", "CIS"), "매출총이익": ("IS", "CIS"),
                "당기순이익": ("IS", "CIS"), "영업활동으로인한현금흐름": ("CF",)}

# 매출총이익을 따로 안 내는 회사가 많습니다 (매출액 - 매출원가로 만듭니다)
COST_OF_SALES = ("CostOfSales",)
COST_OF_SALES_NM = ("매출원가",)


def _tag(account_id) -> str:
    """'ifrs-full_Assets' / 'ifrs_Assets' / 'dart_Assets' → 'Assets'."""
    s = str(account_id or "").strip()
    return s.rsplit("_", 1)[-1] if "_" in s else s


def _norm(s) -> str:
    """계정명 비교용 — 공백과 괄호 안 주석을 지웁니다."""
    return "".join(str(s or "").split())


def _num(row: dict, key: str):
    v = str(row.get(key) or "").replace(",", "").strip()
    if v in ("", "-", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


# 보고서코드 → 그 보고서가 덮는 분기
REPRT_QUARTER = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def _amount(row: dict, *, stock: bool, reprt: str = ""):
    """(값, 기준) 반환. 기준은 'q3m'(그 분기 3개월) 또는 'cum'(연초부터 누적).

    fnlttSinglAcntAll은 금액 컬럼이 둘입니다.
      · thstrm_add_amount — 손익·현금흐름의 연초부터 누적
      · thstrm_amount     — 분기보고서에서는 그 분기 3개월, 사업보고서에서는 1년
    둘 중 무엇을 읽었는지 기억해둬야 나중에 3개월치로 환산할 때 틀리지 않습니다.
    (누적을 3개월로 착각해 빼면 완전히 엉뚱한 값이 나옵니다.)
    """
    if stock:
        v = _num(row, "thstrm_amount")
        return (v, "point") if v is not None else (None, None)

    v = _num(row, "thstrm_add_amount")
    if v is not None:
        return v, "cum"
    v = _num(row, "thstrm_amount")
    if v is None:
        return None, None
    # 사업보고서의 thstrm_amount는 1년치 = 4분기 누적
    return v, ("cum" if str(reprt) == "11011" else "q3m")


def _period_end(row: dict, year: int | None = None, quarter: int | None = None):
    """기간 종료일.

    fnlttSinglAcntAll 응답에는 날짜 컬럼이 없습니다(thstrm_dt는 '주요계정' API에만
    있습니다). 그래서 응답이 들고 있는 사업연도·보고서코드로 분기말을 만듭니다.
    12월 결산이 아닌 회사는 이 계산이 틀리므로, 호출하는 쪽에서 미리 걸러냅니다.
    """
    import re
    found = re.findall(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
                       str(row.get("thstrm_dt") or ""))
    if found:
        y, m, d = found[-1]
        try:
            return pd.Timestamp(int(y), int(m), int(d))
        except ValueError:
            pass

    y = row.get("bsns_year") or year
    q = REPRT_QUARTER.get(str(row.get("reprt_code") or ""), quarter)
    if not y or not q:
        return None
    try:
        return (pd.Timestamp(int(y), int(q) * 3, 1)
                + pd.offsets.MonthEnd(0)).normalize()
    except (ValueError, TypeError):
        return None


def _pick(rows: list, ids: tuple, names: tuple, sj: tuple = ()):
    """표준 태그 우선, 없으면 계정명으로 한 행을 고릅니다.

    같은 태그가 여러 표에 나올 수 있어(당기순이익은 손익계산서와 포괄손익
    계산서 양쪽에 있습니다) 기대하는 재무제표 구분을 먼저 봅니다.
    """
    wanted_nm = {_norm(n) for n in names}

    def matches(r, by_tag: bool):
        return (_tag(r.get("account_id")) in ids if by_tag
                else _norm(r.get("account_nm")) in wanted_nm)

    # 태그 > 계정명, 그리고 sj_div는 지정한 순서대로 (손익계산서 > 포괄손익)
    for by_tag in (True, False):
        for div in sj:
            for r in rows:
                if (str(r.get("sj_div") or "").strip().upper() == div
                        and matches(r, by_tag)):
                    return r
        for r in rows:                      # sj를 못 맞추면 아무 표에서나
            if matches(r, by_tag):
                return r
    return None


def parse_report(payload: dict, ticker: str, quarter: int,
                 year: int | None = None, reprt: str = "") -> dict:
    """DART 응답 하나 → {계정: (기준일, 값, 기준)}. 값은 억원.

    기준은 'point'(잔액) · 'q3m'(그 분기 3개월) · 'cum'(연초부터 누적).
    """
    if str(payload.get("status")) != "000":
        return {}
    rows = payload.get("list") or []
    if not rows:
        return {}

    out: dict[str, tuple] = {}
    for account, (ids, names) in TARGETS.items():
        row = _pick(rows, ids, names, PREFERRED_SJ.get(account, ()))
        if row is None:
            continue
        val, basis = _amount(row, stock=account in STOCK_ACCOUNTS, reprt=reprt)
        end = _period_end(row, year, quarter)
        if val is None or end is None:
            continue
        out[account] = (end, val / UNIT, basis)

    # 매출총이익을 안 내는 회사 — 매출액에서 매출원가를 뺍니다
    if "매출총이익" not in out and "매출액" in out:
        cost = _pick(rows, COST_OF_SALES, COST_OF_SALES_NM, ("IS", "CIS"))
        if cost is not None:
            cv, cbasis = _amount(cost, stock=False, reprt=reprt)
            end, rev, rbasis = out["매출액"]
            if cv is not None and cbasis == rbasis:   # 같은 기준일 때만
                out["매출총이익"] = (end, rev - cv / UNIT, rbasis)
    return out


def to_quarterly(by_quarter: dict, ticker: str) -> pd.DataFrame:
    """{분기: {계정: (기준일, 값, 기준)}} → 3개월치 kor_fs 행.

    누적으로 받은 값은 직전 분기 누적을 빼서 3개월치로 만듭니다. 보고서마다
    누적으로 오기도 하고 3개월로 오기도 해서, 일단 전부 누적으로 환산한 뒤
    차분합니다. 앞 분기를 모르면 그 분기는 버립니다(틀린 값보다 빈 값).
    """
    recs = []
    cum: dict[str, dict[int, float]] = {}      # 계정 → {분기: 연초부터 누적}

    for q in (1, 2, 3, 4):
        cur = by_quarter.get(q)
        if not cur:
            continue
        for account, (end, val, basis) in cur.items():
            if basis == "point":               # 잔액 — 차분 안 함
                amount = val
            elif basis == "q3m":               # 이미 3개월치
                amount = val
                prev_cum = cum.get(account, {}).get(q - 1, 0.0 if q == 1 else None)
                if prev_cum is not None:
                    cum.setdefault(account, {})[q] = prev_cum + val
            else:                              # 누적
                cum.setdefault(account, {})[q] = val
                prev_cum = 0.0 if q == 1 else cum.get(account, {}).get(q - 1)
                if prev_cum is None:
                    continue                   # 차분할 앞 분기가 없음
                amount = val - prev_cum
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


def _fetch_year(api_key: str, ticker: str, corp: str, year: int) -> dict:
    """한 종목·한 사업연도의 4개 보고서를 받아 정리합니다 (작업 단위)."""
    by_q, got, used_total = {}, 0, 0
    statuses: dict[str, int] = {}
    sample = None

    for reprt, q, _months in REPORTS:
        data, used = fetch_report(api_key, corp, year, reprt)
        used_total += used
        st = str(data.get("status"))
        statuses[st] = statuses.get(st, 0) + 1
        parsed = parse_report(data, ticker, q, year, reprt)
        if parsed:
            by_q[q] = parsed
            got += 1
        elif sample is None and st == "000" and data.get("list"):
            # 응답은 정상인데 계정을 못 찾았습니다. 무엇이 왔는지 그대로
            # 남겨야 매핑을 고칠 수 있습니다.
            sample = {"종목코드": ticker, "연도": year, "보고서": reprt,
                      "행수": len(data["list"]), "예시": data["list"][:25]}

    return {"종목코드": ticker, "연도": int(year), "분기": by_q, "성공": got,
            "호출": used_total, "상태": statuses, "진단": sample}


# ── 진행 상태 ────────────────────────────────────────────────
def load_progress() -> pd.DataFrame:
    df = store.read(PROGRESS)
    if df.empty:
        return pd.DataFrame(columns=["종목코드", "연도", "상태", "갱신일"])
    return df


def _done_keys(prog: pd.DataFrame) -> set:
    """완료로 볼 종목·연도. 한 분기도 못 읽은 건(0/4)은 다시 시도합니다.

    계정 매핑이 틀려서 아무것도 못 받은 경우까지 '완료'로 남기면, 고친 뒤에도
    영원히 건너뛰게 됩니다. 빈손으로 끝난 건은 완료로 치지 않습니다.
    """
    if prog.empty:
        return set()
    ok = prog[prog["상태"].astype(str) != "0/4"] if "상태" in prog.columns else prog
    return set(zip(ok["종목코드"].astype(str), ok["연도"].astype(int)))


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
    dec_fye = december_filers()
    prog = load_progress()
    done = _done_keys(prog)
    odd_fiscal = 0

    calls, buffer, new_prog = 1, [], []
    rows_saved, skipped, missing = 0, 0, 0
    today = pd.Timestamp.today().normalize()
    sample = None          # 아무것도 못 읽었을 때 원인을 보여줄 실제 응답
    statuses: dict[str, int] = {}

    # 받을 목록을 먼저 만듭니다 (이미 받은 건·DART에 없는 종목·비12월 결산 제외)
    tasks = []
    for ticker in tickers:
        corp = cmap.get(ticker)
        if corp is None:
            missing += 1
            continue
        if dec_fye is not None and ticker not in dec_fye:
            # 응답에 날짜가 없어 사업연도·보고서코드로 분기말을 계산합니다.
            # 12월 결산이 아니면 그 계산이 틀리므로 아예 건너뜁니다.
            odd_fiscal += 1
            continue
        for year in years:
            if (ticker, year) in done:
                skipped += 1
                continue
            tasks.append((ticker, corp, int(year)))

    # DART 응답이 한 건에 3~4초씩 걸립니다(전체 재무제표라 200행 안팎).
    # 순차로 돌리면 하루 한도를 다 쓰기도 전에 작업 시간(6시간)이 먼저 끝납니다.
    # 동시에 여러 건을 받아 시간 안에 한도를 다 쓰도록 합니다.
    lock = threading.Lock()
    stop = {"why": None}
    MAX_PER_TASK = len(REPORTS) * 2      # 4보고서 × (연결 실패 시 별도)

    def work(task):
        nonlocal calls
        ticker, corp, year = task
        with lock:
            if stop["why"]:
                return None
            if calls + MAX_PER_TASK > budget:
                stop["why"] = "호출 한도"
                return None
            calls += MAX_PER_TASK              # 최악을 먼저 잡아둡니다
        try:
            res = _fetch_year(api_key, ticker, corp, year)
        except RuntimeError as e:              # 한도 초과·인증키 오류
            with lock:
                calls -= MAX_PER_TASK
                stop["why"] = str(e)
            return None
        except Exception as e:                 # 네트워크 등 개별 실패는 건너뜁니다
            with lock:
                calls -= MAX_PER_TASK
            log.warning("DART 수집 실패 %s %s: %s", ticker, year, e)
            return None
        with lock:
            calls -= MAX_PER_TASK - res["호출"]   # 안 쓴 만큼 돌려놓습니다
        return res

    with ThreadPoolExecutor(max_workers=config.DART_WORKERS) as pool:
        for res in pool.map(work, tasks):
            if res is None:
                continue
            for k, v in res["상태"].items():
                statuses[k] = statuses.get(k, 0) + v
            if sample is None and res["진단"]:
                sample = res["진단"]
            if res["분기"]:
                df = to_quarterly(res["분기"], res["종목코드"])
                if len(df):
                    buffer.append(df)
            new_prog.append({"종목코드": res["종목코드"], "연도": res["연도"],
                             "상태": f"{res['성공']}/4", "갱신일": today})

            if len(buffer) >= chunk:
                rows_saved += store.upsert("kor_fs", pd.concat(buffer, ignore_index=True))
                buffer = []
                save_progress(prog, new_prog)
                prog, new_prog = load_progress(), []
                log.info("DART 수집 %d회 호출 · %d행 누적 · 남은작업 %d",
                         calls, rows_saved, len(tasks) - len(new_prog))

    stopped = stop["why"]

    if buffer:
        rows_saved += store.upsert("kor_fs", pd.concat(buffer, ignore_index=True))
    total = save_progress(prog, new_prog)

    remaining = len(tickers) * len(years) - total
    out = {"호출수": calls, "저장행수": rows_saved, "완료": total,
           "남은작업": max(remaining, 0), "건너뜀": skipped,
           "DART에없음": missing, "12월결산아님": odd_fiscal,
           "응답상태": statuses, "중단사유": stopped or "완료"}
    if rows_saved == 0 and sample is not None:
        out["진단"] = sample
    return out


def december_filers() -> set | None:
    """12월 결산 종목 집합. 기존 FnGuide 데이터의 분기말 월로 판정합니다.

    DART 호출을 한 번도 더 쓰지 않는 방법입니다 — 이미 받아둔 kor_fs에
    각 종목의 분기 기준일이 들어 있고, 12월 결산이면 3·6·9·12월에만 찍힙니다.
    판정할 근거가 없으면 None(=전부 허용)을 돌려줍니다.
    """
    if not store.exists("kor_fs"):
        return None
    try:
        df = store.read_sql("""
            select 종목코드, month(기준일) 월 from kor_fs
            where 공시구분 = 'q' group by 1, 2;
        """)
    except Exception:
        return None
    if df.empty:
        return None
    months = df.groupby("종목코드")["월"].apply(set)
    ok = {str(t).zfill(6) for t, ms in months.items() if ms <= {3, 6, 9, 12}}
    return ok or None


def _priority_tickers(limit: int | None = None) -> list[str]:
    """유동성 높은 종목부터, 상위 limit개만.

    전 종목 × 11년이면 호출이 16만 회(=8일)입니다. 실제 백테스트는 20일 평균
    거래대금 5억 이상만 사는데 그게 700종목 안팎이라, 하위 종목에 호출을 쓰는
    건 대부분 낭비입니다.

    다만 정직하게 밝힐 점: 순서를 '지금' 거래대금으로 매기므로, 과거엔 활발했는데
    지금은 죽은 종목이 빠집니다. 표본에 약간의 선택편향이 들어갑니다.
    여유를 두고 자르는 이유입니다(필터 통과 ~700 대비 기본 1,200).
    """
    limit = config.DART_MAX_TICKERS if limit is None else limit
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
        return tickers[:limit] if limit else tickers
    keep = set(tickers)
    order = [t for t in rank["종목코드"].astype(str).str.zfill(6) if t in keep]
    order += [t for t in tickers if t not in set(order)]
    return order[:limit] if limit else order


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(collect())
