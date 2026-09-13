# -*- coding: utf-8 -*-
"""밸류 지표 계산 (PER/PBR/PCR/PSR — TTM 기반, DY)."""
import numpy as np
import pandas as pd

import factor_core as fc
import store

ACCOUNT_TO_METRIC = {
    "매출액": "PSR",
    "영업활동으로인한현금흐름": "PCR",
    "자본": "PBR",
    "당기순이익": "PER",
}


def build() -> dict:
    kor_fs = store.read_sql("""
        select * from kor_fs
        where 공시구분 = 'q'
          and 계정 in ('당기순이익', '자본', '영업활동으로인한현금흐름', '매출액');
    """)
    ticker_list = store.read_sql("""
        select * from kor_ticker
        where 기준일 = (select max(기준일) from kor_ticker)
          and 종목구분 = '보통주';
    """)

    if kor_fs.empty:
        raise RuntimeError("kor_fs(분기 재무제표)가 비어 있습니다. 월간 수집을 먼저 실행하세요.")

    # TTM (최근 4개 분기 합; 자본은 평균, 분기 연속성 검사 포함)
    kor_fs = fc.latest_ttm(kor_fs, symbol="종목코드", account="계정",
                           date="기준일", value="값", mean_accounts=("자본",))

    # 시가총액 / TTM
    merged = kor_fs[["계정", "종목코드", "ttm"]].merge(
        ticker_list[["종목코드", "시가총액", "기준일"]], on="종목코드")
    merged["시가총액"] = merged["시가총액"] / 1e8      # 억원 단위
    merged["값"] = (merged["시가총액"] / merged["ttm"]).round(4)
    merged["지표"] = merged["계정"].map(ACCOUNT_TO_METRIC)
    merged = merged[["종목코드", "기준일", "지표", "값"]]
    merged = merged.replace([np.inf, -np.inf], np.nan).dropna(subset=["값"])
    n_value = store.upsert("kor_value", merged)

    # 배당수익률
    tl = ticker_list.copy()
    tl["값"] = (tl["주당배당금"] / tl["종가"]).round(4)
    tl["지표"] = "DY"
    dy = tl[["종목코드", "기준일", "지표", "값"]]
    dy = dy.replace([np.inf, -np.inf], np.nan).dropna(subset=["값"])
    dy = dy[dy["값"] != 0]
    n_dy = store.upsert("kor_value", dy)

    return {"value_rows": n_value, "dy_rows": n_dy}


if __name__ == "__main__":
    print(build())
