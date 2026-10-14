# -*- coding: utf-8 -*-
"""SEC EDGAR XBRL에서 미국 분기 재무제표를 받아 global_fs에 넣습니다.

    python tools/sec_facts.py probe [종목수]     # 구조·커버리지만 보고 (쓰지 않음)
    python tools/sec_facts.py collect            # 전 종목 수집 후 저장

왜 바꾸는가 (실측)
------------------------------------------------------------------
yahooquery는 종목당 **최근 5분기쯤의 실제 값만** 줍니다. 그 이전 분기는 행은
생기지만 값이 비어 있습니다 — 2023Q4~2025Q1 구간의 87~98%가 null이었습니다.
그래서 4분기 TTM이 2026Q1에야 완성되고, 공시지연 90일을 더하면 미국 모델이
쓸 만한 유니버스를 갖는 건 2026-06-30부터였습니다. 3개월 반짜리 백테스트로는
전략을 판정할 수 없습니다.

EDGAR는 공시 원본이라 10년 이상이 들어 있고, API 키가 필요 없습니다.

두 가지 중요한 설계
------------------------------------------------------------------
1. **처음 보고된 값을 씁니다** (가장 이른 filed). 같은 분기를 나중에 정정한
   값으로 과거를 테스트하면, 그때는 알 수 없었던 숫자로 매매한 셈이 됩니다
   (사후확신 편향). yahooquery는 정정된 현재 값만 주므로 이걸 피할 수 없었습니다.
2. **filed(실제 공시일)를 함께 저장합니다.** 지금 백테스트는 '공시지연 90일'
   이라는 가정을 쓰는데, 실제 공시일이 있으면 가정 없이 그 시점에 정말
   알려졌던 것만 쓸 수 있습니다. (이번 변경은 저장까지만 하고, 백테스트가
   이걸 쓰도록 바꾸는 건 다음 단계입니다.)
"""
import io
import json
import logging
import os
import sys
import zipfile
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import store  # noqa: E402

log = logging.getLogger("quant_agent.sec_facts")

_TODAY = pd.Timestamp.today().normalize()

# SEC는 접속 주체를 밝히는 User-Agent를 **반드시** 요구하고, 공식 예시가
#     Sample Company Name AdminContact@<sample company domain>.com
# 입니다. 즉 이름 + 연락 이메일입니다. 이메일 없이 보내면 "Undeclared
# Automated Tool"로 막혀 403이 돌아옵니다 (실측: 2026-10-14 런이 그렇게
# 실패했습니다 — 이름만 넣고 이메일을 안 넣었습니다).
#
# 개인 메일을 코드에 박지 않습니다. 레포 Variables의 QUANT_SEC_UA로 받습니다.
# 접속 속도 상한은 초당 10회인데 이 스크립트는 전체에서 요청이 3번뿐입니다
# (티커 목록 + 번들 zip).
UA_ENV = "QUANT_SEC_UA"
UA_HELP = (
    f"SEC는 연락처가 담긴 User-Agent를 요구합니다. 레포 Settings > Secrets and "
    f"variables > Actions > Variables 에 {UA_ENV} 를 넣으세요.\n"
    f'  예:  {UA_ENV} = "SangsooKim quant research you@example.com"\n'
    f"이름과 **이메일**이 둘 다 있어야 합니다 — 이메일이 없으면 403으로 막힙니다 "
    f"(SEC 공식 예시: 'Sample Company Name AdminContact@domain.com')."
)


def _clean_ua(raw: str) -> str:
    """붙여넣기 실수를 견디게 다듬습니다.

    HTTP 헤더 값에는 줄바꿈이 들어갈 수 없습니다. 그런데 설정 안내를 그대로
    복사하면 'Name: QUANT_SEC_UA\nValue: ...' 두 줄이 통째로 들어옵니다
    (실측: 2026-10-14 런이 그렇게 죽었습니다 — requests가 'Invalid ...
    return character(s) in header value'로 거부). 그 경우 Value: 뒤만 씁니다.
    """
    txt = (raw or "").replace("\r", "\n")
    lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
    if not lines:
        return ""
    # 'Value:' 줄이 있으면 그 뒤만 — 없으면 변수 이름 줄만 걸러냅니다
    picked = None
    for ln in lines:
        low = ln.lower()
        if low.startswith("value:"):
            picked = ln.split(":", 1)[1]
            break
    if picked is None:
        body = [ln for ln in lines
                if not ln.lower().startswith("name:")
                and ln.strip() != UA_ENV]
        picked = body[0] if body else lines[-1]
    # 남은 제어문자를 지우고 공백을 하나로
    picked = "".join(ch if ch.isprintable() else " " for ch in picked)
    return " ".join(picked.split())


def _ua() -> str:
    ua = _clean_ua(os.getenv(UA_ENV))
    if not ua:
        raise RuntimeError("User-Agent가 설정되지 않았습니다.\n" + UA_HELP)
    if UA_ENV in ua:
        raise RuntimeError(
            f"User-Agent에 변수 이름이 섞여 있습니다: {ua!r}\n"
            f"Value 칸에는 **값만** 넣으세요.\n" + UA_HELP)
    if "@" not in ua or "." not in ua.split("@")[-1]:
        raise RuntimeError(f"User-Agent에 이메일이 없습니다: {ua!r}\n" + UA_HELP)
    return ua


def _headers() -> dict:
    return {"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, */*"}

TICKER_URLS = (
    "https://www.sec.gov/files/company_tickers.json",
    "https://www.sec.gov/files/company_tickers_exchange.json",
)
FACTS_ZIP = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

# 계정별 태그 후보 — 앞쪽이 우선입니다. 회사마다 쓰는 태그가 달라서
# 하나만 보면 절반이 빕니다.
DURATION_TAGS = {
    "NetIncome": ("NetIncomeLoss", "ProfitLoss",
                  "NetIncomeLossAvailableToCommonStockholdersBasic"),
    # 금융업은 Revenues를 안 쓰고 이자·수수료 수익 태그를 씁니다.
    # probe에서 699개가 비어 있었습니다.
    "TotalRevenue": ("Revenues",
                     "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "RevenueFromContractWithCustomerIncludingAssessedTax",
                     "SalesRevenueNet", "SalesRevenueGoodsNet",
                     "RevenuesNetOfInterestExpense",
                     "TotalRevenuesAndOtherIncome",
                     "RegulatedAndUnregulatedOperatingRevenue",
                     "InterestAndDividendIncomeOperating",
                     "HealthCareOrganizationRevenue",
                     "RealEstateRevenueNet"),
    "GrossProfit": ("GrossProfit",),
    "CashFlowFromContinuingOperatingActivities": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
}
INSTANT_TAGS = {
    "TotalAssets": ("Assets",),
    "StockholdersEquity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
}
# GrossProfit을 안 쓰는 회사가 많습니다(3,262/4,935). 매출 - 매출원가로 만듭니다.
COST_TAGS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
             "CostOfServices", "CostOfSales",
             "CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization",
             "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
             "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization")

ACCOUNTS = tuple(DURATION_TAGS) + tuple(INSTANT_TAGS)
QUARTER_DAYS = (80, 100)       # 3개월 공시로 인정하는 기간
ANNUAL_DAYS = (350, 380)       # 12개월 공시 (Q4를 역산할 때 씁니다)


def _get(url: str, timeout: int = 120) -> bytes:
    import requests
    r = requests.get(url, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.content


def _download(url: str, timeout: int = 1800) -> str:
    """메모리에 다 올리지 않고 파일로 흘려받습니다 — 수 GB일 수 있습니다."""
    import tempfile

    import requests
    fd, path_ = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with requests.get(url, headers=_headers(), timeout=timeout,
                      stream=True) as r:
        r.raise_for_status()
        done = 0
        with open(path_, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if done % (200 << 20) < (1 << 20):
                    log.info("  내려받음 %.0fMB", done / 1e6)
    return path_


def ticker_to_cik() -> dict:
    """{티커: CIK(int)}. 여러 주소를 순서대로 시도합니다."""
    last = None
    for url in TICKER_URLS:
        try:
            raw = json.loads(_get(url, 60).decode("utf-8"))
        except Exception as e:                      # 주소가 바뀌었을 수 있습니다
            last = e
            if "403" in str(e):
                raise RuntimeError(
                    f"SEC가 403으로 막았습니다 ({url}). 거의 항상 "
                    f"User-Agent 문제입니다.\n" + UA_HELP) from e
            log.warning("티커 목록 실패 %s: %s", url, e)
            continue
        out = {}
        if isinstance(raw, dict) and "fields" in raw and "data" in raw:
            idx = {f: i for i, f in enumerate(raw["fields"])}
            for row in raw["data"]:
                t = str(row[idx["ticker"]]).upper().strip()
                if t:
                    out[t] = int(row[idx["cik"]])
        else:
            for v in raw.values():
                t = str(v.get("ticker", "")).upper().strip()
                if t:
                    out[t] = int(v["cik_str"])
        if out:
            log.info("티커→CIK %d개 (%s)", len(out), url)
            return out
    raise RuntimeError(f"티커→CIK 목록을 받지 못했습니다. 마지막 오류: {last}")


def _series(facts: dict, tags, *, instant: bool = False,
            days_range: tuple | None = None) -> list:
    """태그 후보를 순서대로 보고 첫 번째로 값이 있는 것을 씁니다.

    돌려주는 것: [(기간끝 Timestamp, 값, 공시일 Timestamp, 기간일수)]
    같은 기간이 여러 번 보고되면 **가장 이른 공시**만 남깁니다(사후확신 방지).

    days_range로 **먼저 거른 다음** 기간끝으로 중복을 없앱니다. 순서가
    중요합니다 (실측 버그): 10-Q 하나에는 같은 기간끝에 대해 3개월·6개월·
    9개월 누적이 함께 들어 있고 공시일이 모두 같습니다. 거르기 전에 중복을
    없애면 누적분이 이겨서 남았다가 3개월 필터에서 통째로 버려집니다.
    그래서 AAPL의 분기가 '회계 1분기(12월말)만 1년에 하나'가 됐고, 4분기
    연속이 영영 안 만들어져 2025년 이전 TTM이 전부 0이었습니다.
    """
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        rows = {}
        for unit, entries in (node.get("units") or {}).items():
            if not unit.startswith("USD"):
                continue
            for e in entries:
                end, val, filed = e.get("end"), e.get("val"), e.get("filed")
                if end is None or val is None or filed is None:
                    continue
                # 미래 날짜 공시는 버립니다. probe에서 StockholdersEquity의
                # 기간말이 2026-12까지 나왔는데(실행일 2026-10-14) 그런 값이
                # TTM에 섞이면 '아직 오지 않은 분기'로 과거를 테스트합니다.
                if pd.Timestamp(end) > _TODAY or pd.Timestamp(filed) > _TODAY:
                    continue
                if instant:
                    days = 0
                    if e.get("start"):
                        continue            # 순간 계정에 기간 공시가 섞인 것
                else:
                    if not e.get("start"):
                        continue
                    days = (pd.Timestamp(end) - pd.Timestamp(e["start"])).days
                    if days_range and not (days_range[0] <= days <= days_range[1]):
                        continue
                key = end
                prev = rows.get(key)
                if prev is None or pd.Timestamp(filed) < prev[2]:
                    rows[key] = (pd.Timestamp(end), float(val),
                                 pd.Timestamp(filed), days)
        if rows:
            return sorted(rows.values(), key=lambda r: r[0])
    return []


def _quarterly(quarters: list, annual: list) -> list:
    """3개월 공시 + 연간에서 역산한 Q4.

    많은 회사가 4분기를 따로 공시하지 않고 연간(10-K)만 냅니다. 그걸 빼면
    4분기 연속이 끊겨 TTM이 통째로 무효가 됩니다. 연간에서 앞 3분기를 빼
    Q4를 만듭니다.
    """
    q = {r[0]: r for r in quarters}
    for end, val, filed, _days in annual:
        if end in q:
            continue
        # 같은 회계연도의 앞 3분기 (연말에서 거꾸로 세 분기)
        prev = [r for r in q.values()
                if 0 < (end - r[0]).days <= 290]
        if len(prev) < 3:
            continue
        prev = sorted(prev, key=lambda r: r[0])[-3:]
        q4 = val - sum(r[1] for r in prev)
        q[end] = (end, q4, max(filed, max(r[2] for r in prev)), 92)
    return sorted(q.values(), key=lambda r: r[0])


def extract(facts: dict) -> dict:
    """회사 하나의 companyfacts JSON → {계정: [(기간끝, 값, 공시일)]}"""
    out = {}
    for acct, tags in DURATION_TAGS.items():
        rows = _quarterly(_series(facts, tags, days_range=QUARTER_DAYS),
                          _series(facts, tags, days_range=ANNUAL_DAYS))
        if rows:
            out[acct] = [(r[0], r[1], r[2]) for r in rows]
    for acct, tags in INSTANT_TAGS.items():
        rows = _series(facts, tags, instant=True)
        if rows:
            out[acct] = [(r[0], r[1], r[2]) for r in rows]

    # 매출총이익이 없으면 매출 - 매출원가
    if "GrossProfit" not in out and "TotalRevenue" in out:
        cost = _quarterly(_series(facts, COST_TAGS, days_range=QUARTER_DAYS),
                          _series(facts, COST_TAGS, days_range=ANNUAL_DAYS))
        if cost:
            cmap = {r[0]: r[1] for r in cost}
            gp = [(d, v - cmap[d], f) for d, v, f in out["TotalRevenue"]
                  if d in cmap]
            if gp:
                out["GrossProfit"] = gp
    return out


def _rows(symbol: str, got: dict) -> list:
    return [{"Symbol": symbol, "date": d, "account": acct, "value": v,
             "freq": "q", "filed": f}
            for acct, series in got.items() for d, v, f in series]


def run(mode: str = "probe", limit: int = 0) -> dict:
    _ua()                     # 요청 전에 먼저 막습니다
    want = {str(s).upper() for s in store.us_symbols()}
    if not want:
        raise RuntimeError("global_ticker가 비어 있습니다. 미국 월간 수집을 먼저 "
                           "돌리세요 (티커가 있어야 어느 회사를 뽑을지 압니다).")
    t2c = ticker_to_cik()
    pairs = {t: c for t, c in t2c.items() if t in want}
    log.info("대상 %d종목 (보유 티커 %d개 중 CIK를 찾은 것)", len(pairs), len(want))

    pre_dropped = 0
    if mode == "collect":
        cur = store.read("global_fs")
        if not cur.empty and cur["value"].isna().any():
            keep = cur[cur["value"].notna()]
            pre_dropped = int(len(cur) - len(keep))
            store.write("global_fs", keep)
            log.info("값이 빈 행 %d개를 먼저 삭제 (%d → %d행) — 이후 저장이 "
                     "빨라지고, 중간에 멈춰도 쓰레기가 남지 않습니다",
                     pre_dropped, len(cur), len(keep))
        del cur

    log.info("companyfacts.zip 내려받는 중 — 큰 파일입니다")
    zip_path = _download(FACTS_ZIP)
    log.info("받은 크기 %.0fMB", os.path.getsize(zip_path) / 1e6)

    cik2sym = defaultdict(list)
    for t, c in pairs.items():
        cik2sym[c].append(t)

    found, missing_tag, all_six = 0, defaultdict(int), 0
    span = defaultdict(lambda: [None, None])
    rows, written = [], 0
    with zipfile.ZipFile(zip_path) as zf:
        names = {n.upper(): n for n in zf.namelist()}
        todo = sorted(cik2sym)
        if limit:
            todo = todo[:limit]
        for i, cik in enumerate(todo, 1):
            member = names.get(f"CIK{cik:010d}.JSON")
            if member is None:
                continue
            try:
                facts = json.loads(zf.read(member))
            except Exception:
                continue
            got = extract(facts)
            if not got:
                continue
            found += 1
            if all(a in got for a in ACCOUNTS):
                all_six += 1
            for acct in ACCOUNTS:
                if acct not in got:
                    missing_tag[acct] += 1
                else:
                    lo, hi = span[acct]
                    d0, d1 = got[acct][0][0], got[acct][-1][0]
                    span[acct] = [min(lo, d0) if lo else d0,
                                  max(hi, d1) if hi else d1]
            for sym in cik2sym[cik]:
                rows.extend(_rows(sym, got))
            if mode == "collect" and len(rows) >= 200_000:
                written += store.upsert("global_fs", pd.DataFrame(rows))
                rows.clear()
                log.info("  %d/%d 저장 중 (누적 %d행)", i, len(todo), written)
            if i % 500 == 0:
                log.info("  %d/%d 처리", i, len(todo))

    if mode == "collect" and rows:
        written += store.upsert("global_fs", pd.DataFrame(rows))

    stale = 0
    if mode == "collect":
        # EDGAR가 덮은 (종목, 계정)의 yahooquery 잔존 행을 지웁니다.
        # 같은 분기인데 날짜가 며칠 달라(회계분기말 vs 월말) 둘 다 남고,
        # TTM이 같은 분기를 두 번 더합니다 — 실측 8,188개 조합이 겹쳤습니다.
        cur = store.read("global_fs")
        if not cur.empty and "filed" in cur.columns:
            isq = cur["freq"] == "q"
            have = set(map(tuple, cur.loc[isq & cur["filed"].notna(),
                                          ["Symbol", "account"]]
                           .drop_duplicates().itertuples(index=False, name=None)))
            if have:
                pair = list(zip(cur["Symbol"], cur["account"]))
                covered = pd.Series([pr in have for pr in pair], index=cur.index)
                kill = isq & cur["filed"].isna() & covered
                stale = int(kill.sum())
                if stale:
                    store.write("global_fs", cur[~kill])
                    log.info("EDGAR가 덮은 (종목·계정)의 옛 출처 행 %d개 삭제 "
                             "— 같은 분기를 두 번 더하는 것을 막습니다", stale)
        del cur

    dropped = pre_dropped
    if mode == "collect":
        # 값이 null인 행은 정보가 없는데 4분기 TTM 연속성을 깨뜨립니다.
        # yahooquery가 남긴 빈 행(2023Q4~2025Q1의 87~98%)이 그것입니다.
        cur = store.read("global_fs")
        if not cur.empty:
            keep = cur[cur["value"].notna()]
            dropped += int(len(cur) - len(keep))
            if len(cur) != len(keep):
                store.write("global_fs", keep)
                log.info("값이 빈 행 %d개 삭제 (TTM 연속성을 깨뜨립니다)", dropped)

    out = {"대상종목": len(pairs), "처리한회사": found,
           "6계정_모두있는회사": all_six,
           "계정별_태그없는회사": dict(missing_tag),
           "계정별_기간": {k: f"{v[0]:%Y-%m} ~ {v[1]:%Y-%m}"
                       for k, v in span.items() if v[0]},
           "저장행수": written if mode == "collect" else 0,
           "빈값삭제행수": dropped,
           "옛출처삭제행수": stale,
           "모드": mode}
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else (300 if mode == "probe" else 0)
    if mode not in ("probe", "collect"):
        print(__doc__)
        sys.exit(2)
    res = run(mode, lim)
    print("\n" + "=" * 62)
    for k, v in res.items():
        print(f"  {k}: {v}")
    sys.exit(0)
