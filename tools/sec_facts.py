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

# SEC는 접속 주체를 밝히는 User-Agent를 요구합니다. 개인 메일을 코드에 박지
# 않고 환경변수로 받습니다 — 안 주면 레포 주소로 식별합니다.
UA = os.getenv("QUANT_SEC_UA",
               "quant_cloud research script (github.com/lovesoccers42-cmyk/quant)")
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

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
    "TotalRevenue": ("Revenues",
                     "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "RevenueFromContractWithCustomerIncludingAssessedTax",
                     "SalesRevenueNet", "SalesRevenueGoodsNet"),
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
             "CostOfServices")

ACCOUNTS = tuple(DURATION_TAGS) + tuple(INSTANT_TAGS)
QUARTER_DAYS = (80, 100)       # 3개월 공시로 인정하는 기간
ANNUAL_DAYS = (350, 380)       # 12개월 공시 (Q4를 역산할 때 씁니다)


def _get(url: str, timeout: int = 120) -> bytes:
    import requests
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content


def _download(url: str, timeout: int = 1800) -> str:
    """메모리에 다 올리지 않고 파일로 흘려받습니다 — 수 GB일 수 있습니다."""
    import tempfile

    import requests
    fd, path_ = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with requests.get(url, headers=HEADERS, timeout=timeout, stream=True) as r:
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


def _series(facts: dict, tags, instant: bool) -> list:
    """태그 후보를 순서대로 보고 첫 번째로 값이 있는 것을 씁니다.

    돌려주는 것: [(기간끝 Timestamp, 값, 공시일 Timestamp, 기간일수)]
    같은 기간이 여러 번 보고되면 **가장 이른 공시**만 남깁니다(사후확신 방지).
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
                if instant:
                    days = 0
                    if e.get("start"):
                        continue            # 순간 계정에 기간 공시가 섞인 것
                else:
                    if not e.get("start"):
                        continue
                    days = (pd.Timestamp(end) - pd.Timestamp(e["start"])).days
                key = end
                prev = rows.get(key)
                if prev is None or pd.Timestamp(filed) < prev[2]:
                    rows[key] = (pd.Timestamp(end), float(val),
                                 pd.Timestamp(filed), days)
        if rows:
            return sorted(rows.values(), key=lambda r: r[0])
    return []


def _quarterly(dur: list) -> list:
    """기간 공시에서 분기 값을 만듭니다 — 3개월 공시 + 연간에서 역산한 Q4.

    많은 회사가 4분기를 따로 공시하지 않고 연간(10-K)만 냅니다. 그걸 빼면
    4분기 연속이 끊겨 TTM이 통째로 무효가 됩니다. 연간에서 앞 3분기를 빼
    Q4를 만듭니다.
    """
    q = {r[0]: r for r in dur if QUARTER_DAYS[0] <= r[3] <= QUARTER_DAYS[1]}
    annual = [r for r in dur if ANNUAL_DAYS[0] <= r[3] <= ANNUAL_DAYS[1]]
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
        rows = _quarterly(_series(facts, tags, instant=False))
        if rows:
            out[acct] = [(r[0], r[1], r[2]) for r in rows]
    for acct, tags in INSTANT_TAGS.items():
        rows = _series(facts, tags, instant=True)
        if rows:
            out[acct] = [(r[0], r[1], r[2]) for r in rows]

    # 매출총이익이 없으면 매출 - 매출원가
    if "GrossProfit" not in out and "TotalRevenue" in out:
        cost = _quarterly(_series(facts, COST_TAGS, instant=False))
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
    want = {str(s).upper() for s in store.us_symbols()}
    if not want:
        raise RuntimeError("global_ticker가 비어 있습니다. 미국 월간 수집을 먼저 "
                           "돌리세요 (티커가 있어야 어느 회사를 뽑을지 압니다).")
    t2c = ticker_to_cik()
    pairs = {t: c for t, c in t2c.items() if t in want}
    log.info("대상 %d종목 (보유 티커 %d개 중 CIK를 찾은 것)", len(pairs), len(want))

    log.info("companyfacts.zip 내려받는 중 — 큰 파일입니다")
    zip_path = _download(FACTS_ZIP)
    log.info("받은 크기 %.0fMB", os.path.getsize(zip_path) / 1e6)

    cik2sym = defaultdict(list)
    for t, c in pairs.items():
        cik2sym[c].append(t)

    found, missing_tag = 0, defaultdict(int)
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
            if mode == "collect" and len(rows) >= 400_000:
                written += store.upsert("global_fs", pd.DataFrame(rows))
                rows.clear()
                log.info("  %d/%d 저장 중 (누적 %d행)", i, len(todo), written)
            if i % 500 == 0:
                log.info("  %d/%d 처리", i, len(todo))

    if mode == "collect" and rows:
        written += store.upsert("global_fs", pd.DataFrame(rows))

    dropped = 0
    if mode == "collect":
        # 값이 null인 행은 정보가 없는데 4분기 TTM 연속성을 깨뜨립니다.
        # yahooquery가 남긴 빈 행(2023Q4~2025Q1의 87~98%)이 그것입니다.
        cur = store.read("global_fs")
        if not cur.empty:
            keep = cur[cur["value"].notna()]
            dropped = int(len(cur) - len(keep))
            if dropped:
                store.write("global_fs", keep)
                log.info("값이 빈 행 %d개 삭제 (TTM 연속성을 깨뜨립니다)", dropped)

    out = {"대상종목": len(pairs), "처리한회사": found,
           "계정별_태그없는회사": dict(missing_tag),
           "계정별_기간": {k: f"{v[0]:%Y-%m} ~ {v[1]:%Y-%m}"
                       for k, v in span.items() if v[0]},
           "저장행수": written if mode == "collect" else 0,
           "빈값삭제행수": dropped,
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
