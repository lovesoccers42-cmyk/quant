# -*- coding: utf-8 -*-
"""FnGuide 재무제표 수집 (연간/분기) — 병렬 + 일괄 저장. 월간 실행용."""
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import pandas as pd
from bs4 import BeautifulSoup

import config
import http_util
import store

log = logging.getLogger("quant_agent.fs")

REFERER = "https://comp.fnguide.com/"
CHUNK = 200


def _clean_fs(df, ticker, frequency):
    df = df[~df.loc[:, ~df.columns.isin(["계정"])].isna().all(axis=1)]
    df = df.drop_duplicates(["계정"], keep="first")
    df = pd.melt(df, id_vars="계정", var_name="기준일", value_name="값")
    df = df[~pd.isnull(df["값"])]
    df["계정"] = df["계정"].replace({"계산에 참여한 계정 펼치기": ""}, regex=True)
    df["기준일"] = (pd.to_datetime(df["기준일"], format="%Y/%m", errors="coerce")
                  + pd.tseries.offsets.MonthEnd())
    df = df.dropna(subset=["기준일"])
    df["종목코드"] = ticker
    df["공시구분"] = frequency
    df["값"] = pd.to_numeric(df["값"], errors="coerce")
    return df.dropna(subset=["값"])


def _fetch_one(ticker: str) -> pd.DataFrame:
    url = f"https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp?pGB=1&gicode=A{ticker}"
    page = http_util.get(url, referer=REFERER)
    # FnGuide는 EUC-KR로 내려옵니다. 자동 추정이 틀리면 본문이 깨져 표를 못 찾습니다.
    page.encoding = page.encoding or "euc-kr"
    if (page.encoding or "").lower() in ("iso-8859-1", "ascii"):
        page.encoding = "euc-kr"
    html_text = page.text

    # 같은 응답을 재사용 (요청 1회로 표 파싱 + 결산년 추출)
    data = pd.read_html(StringIO(html_text), displayed_only=False)
    if len(data) < 6:
        # 무엇이 왔는지 리포트에 남깁니다 — 차단 페이지인지 구조 변경인지 갈립니다
        soup = BeautifulSoup(html_text, "html.parser")
        title = (soup.title.get_text(strip=True) if soup.title else "") or "(제목없음)"
        body = " ".join(soup.get_text(" ").split())[:120] or "(본문없음)"
        raise ValueError(
            f"재무제표 표를 찾지 못함 (표 {len(data)}개) "
            f"| HTTP {page.status_code} | 제목[{title}] | 본문[{body}]")

    # 연간
    fs_y = pd.concat([
        data[0].iloc[:, ~data[0].columns.str.contains("전년동기")],
        data[2], data[4],
    ])
    fs_y = fs_y.rename(columns={fs_y.columns[0]: "계정"})

    soup = BeautifulSoup(html_text, "html.parser")
    fiscal = soup.select("div.corp_group1 > h2")[1].text
    fiscal_years = re.findall("[0-9]+", fiscal)

    fs_y = fs_y.loc[:, (fs_y.columns == "계정")
                    | (fs_y.columns.str[-2:].isin(fiscal_years))]
    fs_y_clean = _clean_fs(fs_y, ticker, "y")

    # 분기
    fs_q = pd.concat([
        data[1].iloc[:, ~data[1].columns.str.contains("전년동기")],
        data[3], data[5],
    ])
    fs_q = fs_q.rename(columns={fs_q.columns[0]: "계정"})
    fs_q_clean = _clean_fs(fs_q, ticker, "q")

    return pd.concat([fs_y_clean, fs_q_clean])


def _fetch_with_retry(ticker: str):
    """(티커, 데이터프레임 또는 None, 실패 사유) 반환."""
    try:
        http_util.polite_sleep(config.FS_SLEEP)
        df = _fetch_one(ticker)
        if len(df):
            return ticker, df, None
        return ticker, None, "빈 결과"
    except Exception as e:
        log.debug("재무제표 수집 실패 %s: %s", ticker, e)
        return ticker, None, f"{type(e).__name__}: {str(e)[:120]}"


def collect() -> dict:
    tickers = store.common_tickers()
    if not tickers:
        raise RuntimeError("kor_ticker가 비어 있습니다.")

    errors, buffer, total_rows = [], [], 0
    reasons = Counter()

    with ThreadPoolExecutor(max_workers=config.FS_WORKERS) as pool:
        futures = {pool.submit(_fetch_with_retry, t): t for t in tickers}
        done = 0
        for fut in as_completed(futures):
            ticker, df, why = fut.result()
            if df is None:
                errors.append(ticker)
                reasons[why or "알 수 없음"] += 1
            else:
                buffer.append(df)
            done += 1

            if len(buffer) >= CHUNK:
                total_rows += store.upsert("kor_fs", pd.concat(buffer, ignore_index=True))
                buffer = []
                log.info("재무제표 수집 %d/%d (실패 %d)", done, len(tickers), len(errors))

    if buffer:
        total_rows += store.upsert("kor_fs", pd.concat(buffer, ignore_index=True))

    if reasons:
        log.warning("재무제표 실패 사유: %s", dict(reasons.most_common(5)))

    return {"tickers": len(tickers), "rows": total_rows,
            "fail_reasons": dict(reasons.most_common(5)), "errors": errors,
            "error_rate": round(len(errors) / max(len(tickers), 1), 4)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print({k: v for k, v in collect().items() if k != "errors"})
