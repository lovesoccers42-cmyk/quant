# -*- coding: utf-8 -*-
"""미국장 QVM 팩터 모델 → 엑셀 + parquet 출력.

노트북(factor-Global)과 같이 기술적 매매 신호 없이 QVM 순위만 냅니다.
계산 정의는 한국장과 공유합니다(factor_core).
"""
from datetime import date

import numpy as np
import pandas as pd

import config
import factor_core as fc
import store

QUALITY_ACCOUNTS = ["NetIncome", "GrossProfit",
                    "CashFlowFromContinuingOperatingActivities",
                    "TotalAssets", "StockholdersEquity"]
MEAN_ACCOUNTS = ("TotalAssets", "StockholdersEquity")


def _load():
    ticker_list = store.read_sql("""
        select * from global_ticker
        where date = (select max(date) from global_ticker);
    """)
    fs_list = store.read_sql("""
        select * from global_fs
        where account in ('NetIncome','GrossProfit',
                          'CashFlowFromContinuingOperatingActivities',
                          'TotalAssets','StockholdersEquity')
          and freq = 'q';
    """)
    value_list = store.read_sql("""
        select * from global_value
        where date = (select max(date) from global_value);
    """)
    price_list = store.read_sql("""
        select Date, Close, Symbol from global_price
        where Date >= (select (select max(Date) from global_price) - interval 1 year);
    """)
    return ticker_list, fs_list, value_list, price_list


def _quality(fs_list: pd.DataFrame) -> pd.DataFrame:
    fs = fc.latest_ttm(fs_list, symbol="Symbol", account="account",
                       date="date", value="value", mean_accounts=MEAN_ACCOUNTS)
    pivot = fs.pivot(index="Symbol", columns="account", values="ttm")
    for col in QUALITY_ACCOUNTS:
        if col not in pivot.columns:
            pivot[col] = np.nan
    pivot["ROE"] = pivot["NetIncome"] / pivot["StockholdersEquity"]
    pivot["GPA"] = pivot["GrossProfit"] / pivot["TotalAssets"]
    pivot["CFO"] = (pivot["CashFlowFromContinuingOperatingActivities"]
                    / pivot["TotalAssets"])
    return pivot


def run() -> dict:
    ticker_list, fs_list, value_list, price_list = _load()

    if price_list.empty:
        raise RuntimeError("global_price가 비어 있습니다.")
    if fs_list.empty:
        raise RuntimeError("global_fs가 비어 있습니다. 미국 월간 실행을 먼저 돌려주세요.")

    fs_pivot = _quality(fs_list)

    value_list = value_list.copy()
    value_list.loc[value_list["값"] <= 0, "값"] = np.nan
    value_pivot = value_list.pivot(index="Symbol", columns="지표", values="값")
    for col in ("PBR", "PCR", "PER", "PSR", "DY"):
        if col not in value_pivot.columns:
            value_pivot[col] = np.nan

    price_pivot = price_list.pivot(index="Date", columns="Symbol", values="Close")
    ret_list, k = fc.momentum(price_pivot)
    k_bind = k.reindex(ticker_list["Symbol"]).reset_index()
    k_bind.columns = ["Symbol", "K_ratio"]

    data_bind = (ticker_list[["Symbol", "Name", "Sector", "Exchange", "Market Cap"]]
                 .merge(fs_pivot[["ROE", "GPA", "CFO"]], how="left", on="Symbol")
                 .merge(value_pivot, how="left", on="Symbol")
                 .merge(ret_list, how="left", on="Symbol")
                 .merge(k_bind, how="left", on="Symbol"))
    data_bind.loc[data_bind["Sector"].isnull(), "Sector"] = "Unknown"
    data_bind = data_bind.drop_duplicates("Symbol")

    port = fc.build_scores(data_bind, symbol="Symbol", sector="Sector",
                           weights=config.QVM_WEIGHTS,
                           n_portfolio=config.N_PORTFOLIO)

    # 어느 팩터에서 종목이 떨어져 나갔는지 리포트에 남깁니다
    coverage = {k: int(port[k].notna().sum())
                for k in ("ROE", "PER", "12M", "K_ratio",
                          "z_quality", "z_value", "z_momentum", "qvm")}

    invest = port[port["invest"] == "Y"].copy()
    invest = invest.sort_values("qvm").reset_index(drop=True).round(4)

    today = f"{date.today():%Y%m%d}"
    xlsx_path = config.OUTPUT_DIR / config.report_filename("미국", today)
    invest.to_excel(xlsx_path, index=False)
    invest.to_parquet(config.OUTPUT_DIR / "model_us_latest.parquet", index=False)

    top = (invest[["Symbol", "Name", "Sector", "qvm",
                   "z_quality", "z_value", "z_momentum"]].head(15))

    return {"universe": len(port), "selected": len(invest),
            "coverage": coverage, "excel": str(xlsx_path), "model_date": today,
            "top_buys": top.to_dict(orient="records")}


if __name__ == "__main__":
    r = run()
    print({k: v for k, v in r.items() if k != "top_buys"})
