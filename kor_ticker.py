# -*- coding: utf-8 -*-
"""티커·기본 지표 수집 (pykrx)."""
import numpy as np
import pandas as pd
from pykrx import stock

import config
import store

COLUMNS = ["종목코드", "종목명", "시장구분", "종가", "시가총액",
           "기준일", "EPS", "BPS", "주당배당금", "종목구분"]


def latest_business_day() -> str:
    """가장 가까운 영업일 (YYYYMMDD)."""
    return stock.get_nearest_business_day_in_a_week()


def collect(biz_day: str | None = None) -> dict:
    """KOSPI/KOSDAQ 티커 + 시세 + 펀더멘털 수집."""
    biz_day = biz_day or latest_business_day()

    frames = []
    for mkt in ("KOSPI", "KOSDAQ"):
        try:
            cap = stock.get_market_cap_by_ticker(biz_day, market=mkt)
            fund = stock.get_market_fundamental_by_ticker(biz_day, market=mkt)
        except Exception as e:
            raise RuntimeError(
                f"KRX에서 {mkt} 데이터를 받지 못했습니다 (기준일 {biz_day}). "
                f"원인: {type(e).__name__}: {e}\n"
                "KRX가 빈 응답이나 HTML을 돌려준 경우입니다. 확인 순서:\n"
                " 1) GitHub Secrets에 KRX_ID / KRX_PW 가 등록돼 있는지 "
                "(pykrx는 계정이 있으면 로그인해서 받아옵니다)\n"
                " 2) 기준일이 휴장일이 아닌지\n"
                " 3) 그래도 실패하면 KRX가 해외 IP를 막은 것입니다"
            ) from e

        m = cap[["종가", "시가총액"]].join(fund[["EPS", "BPS", "DPS"]], how="left")
        m["시장구분"] = mkt
        frames.append(m)

    df = pd.concat(frames)
    if df.empty or len(df) < 100:
        raise RuntimeError(
            f"pykrx가 빈 데이터를 반환했습니다 (기준일 {biz_day}). "
            "KRX가 해외 IP를 차단했거나 휴장일일 수 있습니다."
        )

    df.index.name = "종목코드"
    df = df.reset_index()
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    df["종목명"] = df["종목코드"].map(stock.get_market_ticker_name)
    df = df.rename(columns={"DPS": "주당배당금"})

    # 종목구분: 스팩 / 우선주 / 리츠 / 보통주
    df["종목구분"] = np.where(
        df["종목명"].str.contains("스팩|제[0-9]+호", na=False), "스팩",
        np.where(
            df["종목코드"].str[-1:] != "0", "우선주",
            np.where(df["종목명"].str.endswith("리츠", na=False), "리츠", "보통주"),
        ),
    )

    df["기준일"] = pd.to_datetime(biz_day)
    df = df[COLUMNS]
    for col in ("종가", "시가총액", "EPS", "BPS", "주당배당금"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    n = store.upsert("kor_ticker", df)
    n_common = int((df["종목구분"] == "보통주").sum())
    return {"biz_day": biz_day, "rows": n, "common_stocks": n_common}


if __name__ == "__main__":
    print(collect())
