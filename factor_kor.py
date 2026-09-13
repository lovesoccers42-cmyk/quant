# -*- coding: utf-8 -*-
"""한국장 QVM 팩터 모델 + 기술적 지표(MACD/RSI/볼린저) → 엑셀 + parquet 출력.

계산 정의는 factor_core에 있고 미국장과 공유합니다.
"""
from datetime import date

import numpy as np
import pandas as pd

import config
import factor_core as fc
import indicators as ind
import store

QUALITY_ACCOUNTS = ["당기순이익", "매출총이익", "영업활동으로인한현금흐름", "자산", "자본"]
MEAN_ACCOUNTS = ("자산", "자본")   # 스톡 계정은 합이 아니라 평균


def _load():
    ticker_list = store.read_sql("""
        select * from kor_ticker
        where 기준일 = (select max(기준일) from kor_ticker)
          and 종목구분 = '보통주';
    """)
    fs_list = store.read_sql("""
        select * from kor_fs
        where 계정 in ('당기순이익','매출총이익','영업활동으로인한현금흐름','자산','자본')
          and 공시구분 = 'q';
    """)
    value_list = store.read_sql("""
        select * from kor_value
        where 기준일 = (select max(기준일) from kor_value);
    """)
    price_list = store.read_sql("""
        select 날짜, 종가, 거래량, 종목코드 from kor_price
        where 날짜 >= (select (select max(날짜) from kor_price) - interval 1 year);
    """)
    sector_list = store.read_sql("""
        select * from kor_sector
        where 기준일 = (select max(기준일) from kor_sector);
    """)
    return ticker_list, fs_list, value_list, price_list, sector_list


def _quality(fs_list: pd.DataFrame) -> pd.DataFrame:
    fs = fc.latest_ttm(fs_list, symbol="종목코드", account="계정",
                       date="기준일", value="값", mean_accounts=MEAN_ACCOUNTS)
    pivot = fs.pivot(index="종목코드", columns="계정", values="ttm")
    for col in QUALITY_ACCOUNTS:
        if col not in pivot.columns:
            pivot[col] = np.nan
    pivot["ROE"] = pivot["당기순이익"] / pivot["자본"]
    pivot["GPA"] = pivot["매출총이익"] / pivot["자산"]
    pivot["CFO"] = pivot["영업활동으로인한현금흐름"] / pivot["자산"]
    return pivot


def _technical_signals(price_pivot: pd.DataFrame, codes) -> pd.DataFrame:
    """MACD 크로스 / RSI / 볼린저밴드 → 매수·매도 점수."""
    macd_sig, rsi_last, bb_sig = {}, {}, {}

    for c in codes:
        px = price_pivot[c].dropna()
        if len(px) < 30:
            macd_sig[c], rsi_last[c], bb_sig[c] = "", np.nan, ""
            continue

        try:
            macd_line, signal_line = ind.macd(px)
            osc = np.sign(macd_line - signal_line).dropna()
            if len(osc) >= 2:
                prev, last = osc.iloc[-2], osc.iloc[-1]
                macd_sig[c] = ("Golden Cross" if (last == 1 and prev == -1)
                               else "Death Cross" if (last == -1 and prev == 1)
                               else "")
            else:
                macd_sig[c] = ""
        except Exception:
            macd_sig[c] = ""

        try:
            r = ind.rsi(px, 14).dropna()
            rsi_last[c] = float(r.iloc[-1]) if len(r) else np.nan
        except Exception:
            rsi_last[c] = np.nan

        try:
            lower, _, upper = ind.bbands(px, 20, 2.0)
            close = float(px.iloc[-1])
            up, lo = upper.dropna(), lower.dropna()
            bb_sig[c] = ("Upper" if len(up) and close > float(up.iloc[-1])
                         else "Lower" if len(lo) and close < float(lo.iloc[-1])
                         else "")
        except Exception:
            bb_sig[c] = ""

    macd_s = pd.Series(macd_sig, dtype="object")
    rsi_s = pd.Series(rsi_last, dtype="float64")
    bb_s = pd.Series(bb_sig, dtype="object")

    cnt = ((macd_s == "Golden Cross").astype(int)
           + (rsi_s <= 30).astype(int)
           + (bb_s == "Lower").astype(int)
           - (macd_s == "Death Cross").astype(int)
           - (rsi_s >= 70).astype(int)
           - (bb_s == "Upper").astype(int))

    label = cnt.map({3: "3매수", 2: "2매수", 1: "1매수",
                     -1: "1매도", -2: "2매도", -3: "3매도"}).fillna("")

    return pd.DataFrame({"MACD": macd_s, "RSI": rsi_s.round(2),
                         "BB": bb_s, "매수/매도": label})


def run() -> dict:
    ticker_list, fs_list, value_list, price_list, sector_list = _load()

    if price_list.empty:
        raise RuntimeError("kor_price가 비어 있습니다.")
    if fs_list.empty:
        raise RuntimeError("kor_fs가 비어 있습니다. 월간 실행을 먼저 돌려주세요.")

    fs_pivot = _quality(fs_list)

    value_list = value_list.copy()
    value_list.loc[value_list["값"] <= 0, "값"] = np.nan
    value_pivot = value_list.pivot(index="종목코드", columns="지표", values="값")
    for col in ("PBR", "PCR", "PER", "PSR", "DY"):
        if col not in value_pivot.columns:
            value_pivot[col] = np.nan

    price_pivot = price_list.pivot(index="날짜", columns="종목코드", values="종가")
    ret_list, k = fc.momentum(price_pivot)
    k_bind = k.reindex(ticker_list["종목코드"]).reset_index()
    k_bind.columns = ["종목코드", "K_ratio"]

    data_bind = (ticker_list[["종목코드", "종목명"]]
                 .merge(sector_list[["CMP_CD", "SEC_NM_KOR"]],
                        how="left", left_on="종목코드", right_on="CMP_CD")
                 .merge(fs_pivot[["ROE", "GPA", "CFO"]], how="left", on="종목코드")
                 .merge(value_pivot, how="left", on="종목코드")
                 .merge(ret_list, how="left", on="종목코드")
                 .merge(k_bind, how="left", on="종목코드"))
    data_bind.loc[data_bind["SEC_NM_KOR"].isnull(), "SEC_NM_KOR"] = "기타"
    data_bind = data_bind.drop(["CMP_CD"], axis=1)

    port = fc.build_scores(data_bind, symbol="종목코드", sector="SEC_NM_KOR",
                           weights=config.QVM_WEIGHTS, n_portfolio=len(data_bind))

    coverage = {k: int(port[k].notna().sum())
                for k in ("ROE", "PER", "12M", "K_ratio",
                          "z_quality", "z_value", "z_momentum", "qvm")}

    # 실제로 살 수 있는 종목만 + 한 섹터 쏠림 제한
    eligible, liq = fc.liquidity_eligible(
        price_list, symbol="종목코드", date="날짜", close="종가", volume="거래량",
        window=config.LIQUIDITY_WINDOW, min_value=config.MIN_TURNOVER_KR)

    invest, sel = fc.select_portfolio(
        port, symbol="종목코드", sector="SEC_NM_KOR", n=config.N_PORTFOLIO,
        max_sector_pct=config.MAX_SECTOR_PCT, eligible=eligible)

    codes = [c for c in invest["종목코드"] if c in price_pivot.columns]

    tech = _technical_signals(price_pivot, codes)
    invest = invest.merge(tech, how="left", left_on="종목코드", right_index=True)
    invest = invest.sort_values("qvm").reset_index(drop=True).round(4)

    today = f"{date.today():%Y%m%d}"
    xlsx_path = config.OUTPUT_DIR / config.report_filename("한국", today)
    invest.to_excel(xlsx_path, index=False)
    invest.to_parquet(config.OUTPUT_DIR / "model_kr_latest.parquet", index=False)

    n_buy = int(invest["매수/매도"].str.contains("매수", na=False).sum())
    n_sell = int(invest["매수/매도"].str.contains("매도", na=False).sum())
    top_buys = (invest[invest["매수/매도"].str.contains("매수", na=False)]
                [["종목코드", "종목명", "SEC_NM_KOR", "qvm", "MACD", "RSI", "BB", "매수/매도"]]
                .sort_values("매수/매도", ascending=False).head(15))

    return {"selected": len(invest), "buy_signals": n_buy, "sell_signals": n_sell,
            "coverage": coverage, "유동성": liq, "선정": sel,
            "excel": str(xlsx_path), "model_date": today,
            "top_buys": top_buys.to_dict(orient="records")}


if __name__ == "__main__":
    r = run()
    print({k: v for k, v in r.items() if k != "top_buys"})
