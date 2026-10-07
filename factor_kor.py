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


def run(profile: str | None = None) -> dict:
    """프로필별로 점수와 명단을 만듭니다.

    프로필이 다르면 섹터중립 태도와 섹터 상한이 달라져 선정 종목이 달라집니다.
    산출 파일 이름도 프로필마다 다릅니다 — 섞이면 한쪽 명단으로 다른 쪽
    주문서를 내게 됩니다.
    """
    prof = config.profile(profile)
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

    # 종가를 같이 들고 갑니다 — 주간 주문서에서 '몇 주 살지'를 계산하는 데
    # 필요합니다. 한국 주식은 1주 단위라 배정액을 주가로 나눠야 수량이 나옵니다.
    data_bind = (ticker_list[["종목코드", "종목명", "종가"]]
                 .merge(sector_list[["CMP_CD", "SEC_NM_KOR"]],
                        how="left", left_on="종목코드", right_on="CMP_CD")
                 .merge(fs_pivot[["ROE", "GPA", "CFO"]], how="left", on="종목코드")
                 .merge(value_pivot, how="left", on="종목코드")
                 .merge(ret_list, how="left", on="종목코드")
                 .merge(k_bind, how="left", on="종목코드"))
    data_bind.loc[data_bind["SEC_NM_KOR"].isnull(), "SEC_NM_KOR"] = "기타"
    data_bind = data_bind.drop(["CMP_CD"], axis=1)

    # 변동성 (저변동성 팩터용) 과 종목 단위 추세
    _pf = price_pivot.ffill()
    _dr = _pf.tail(config.VOL_WINDOW + 1).pct_change(fill_method=None)
    data_bind["VOL"] = data_bind["종목코드"].map(_dr.std(ddof=1))
    # 추세는 '새로 살 때'만 거릅니다. 여기서 종목을 빼면 유지 판정 명단
    # (model_kr_hold)에서도 빠져 보유분이 강제 매도되고, 순위 버퍼가
    # 무력화됩니다 — 백테스트에서 회전율이 연 152%→593%로 뛴 원인입니다.
    data_bind["위추세"] = True
    if config.TREND_STOCK_MA > 0:
        _ma = _pf.tail(config.TREND_STOCK_MA).mean()
        data_bind["위추세"] = (data_bind["종목코드"]
                            .map(_pf.iloc[-1] >= _ma).fillna(True).astype(bool))

    port = fc.build_scores(
        data_bind, symbol="종목코드", sector="SEC_NM_KOR",
        weights=(prof["qvm_weights"] or
                 (config.QVML_WEIGHTS if config.USE_LOWVOL
                  else config.QVM_WEIGHTS)),
        n_portfolio=len(data_bind), neutral=prof["sector_neutral"])

    coverage = {k: int(port[k].notna().sum())
                for k in ("ROE", "PER", "12M", "K_ratio",
                          "z_quality", "z_value", "z_momentum", "qvm")}

    # 실제로 살 수 있는 종목만 + 한 섹터 쏠림 제한
    eligible, liq = fc.liquidity_eligible(
        price_list, symbol="종목코드", date="날짜", close="종가", volume="거래량",
        window=config.LIQUIDITY_WINDOW, min_value=config.MIN_TURNOVER_KR)

    # 점수가중은 '전체 유니버스 순위'로 비중을 매깁니다. 상위 100위 안에서만
    # 매기면 1위가 100위의 100배를 받아 백테스트와 달라집니다 (백테스트는 전
    # 종목 순위를 써서 쏠림이 훨씬 완만합니다). 순위와 전체 종목 수를 같이
    # 넘겨 주문서가 같은 비중을 재현하게 합니다.
    port = port.copy()
    port["전체순위"] = port["qvm"].rank(method="first")
    port["전체종목수"] = int(port["qvm"].notna().sum())

    # 교차 분할 — 종목코드 해시로 고정 배정 (백테스트와 동일 규칙).
    # 같은 신호에서 뽑으므로 기대수익이 구조적으로 같고, 소속이 영구히
    # 고정되므로 두 계좌의 보유가 절대 겹치지 않습니다.
    _sp = prof["split"] or {}
    _mod = max(1, int(_sp.get("mod", 1) or 1))
    _shared = _sp.get("shared")
    _shared = None if _shared is None else min(1.0, max(0.0, float(_shared)))
    if _shared is not None and _shared >= 1.0:
        _mod = 1
    if _mod > 1:
        _rem = int(_sp.get("rem", 0) or 0) % _mod
        if _shared is None:
            _mine = port["종목코드"].astype(str).map(
                lambda c: fc.split_bucket(c, _mod) == _rem)
        else:
            _mine = port["종목코드"].astype(str).map(
                lambda c: fc.split_keep(c, _shared, _rem, _mod))
        port = port[_mine]
        log_n = len(port)
    else:
        log_n = len(port)

    buy_pool = port
    if config.TREND_STOCK_MA > 0 and "위추세" in port.columns:
        _up = port["위추세"].fillna(True).astype(bool)
        if int(_up.sum()) >= max(30, config.N_PORTFOLIO):
            buy_pool = port[_up]

    invest, sel = fc.select_portfolio(
        buy_pool, symbol="종목코드", sector="SEC_NM_KOR", n=config.N_PORTFOLIO,
        max_sector_pct=prof["max_sector_pct"], eligible=eligible)

    # 유지 판정용 넓은 명단 (순위 버퍼). 살 때는 위의 invest(상위 100위,
    # 섹터 상한 적용)에서만 고르고, 팔 때는 이 명단 밖으로 밀려나야 팝니다.
    # 100위 경계를 오가는 종목을 매달 팔고 되사는 왕복 거래를 막습니다.
    # 섹터 상한을 여기 적용하지 않는 이유: 상한은 새로 담을 때 지키는 규칙이고,
    # 이미 들고 있는 종목을 억지로 팔아야 할 이유는 아닙니다.
    hold_n = int(round(config.N_PORTFOLIO * (1 + config.RANK_BUFFER)))
    hold_pool = port[port["종목코드"].astype(str).isin(eligible)] if eligible else port
    hold_list = (hold_pool.dropna(subset=["qvm"]).nsmallest(hold_n, "qvm")
                 [["종목코드", "종목명", "SEC_NM_KOR", "qvm"]]
                 .reset_index(drop=True))
    hold_list.to_parquet(config.OUTPUT_DIR / prof["hold_file"], index=False)

    codes = [c for c in invest["종목코드"] if c in price_pivot.columns]

    tech = _technical_signals(price_pivot, codes)
    invest = invest.merge(tech, how="left", left_on="종목코드", right_index=True)
    invest = invest.sort_values("qvm").reset_index(drop=True).round(4)

    today = f"{date.today():%Y%m%d}"
    _tag = "한국" if prof["key"] == "main" else f"한국-{prof['라벨']}"
    xlsx_path = config.OUTPUT_DIR / config.report_filename(
        _tag, today, token=f"model-kr-{prof['key']}")
    invest.to_excel(xlsx_path, index=False)
    invest.to_parquet(config.OUTPUT_DIR / prof["model_file"], index=False)

    n_buy = int(invest["매수/매도"].str.contains("매수", na=False).sum())
    n_sell = int(invest["매수/매도"].str.contains("매도", na=False).sum())
    top_buys = (invest[invest["매수/매도"].str.contains("매수", na=False)]
                [["종목코드", "종목명", "SEC_NM_KOR", "qvm", "MACD", "RSI", "BB", "매수/매도"]]
                .sort_values("매수/매도", ascending=False).head(15))

    _prof_info = {"프로필": prof["key"], "라벨": prof["라벨"],
                  "섹터중립": prof["sector_neutral"],
                  "섹터상한": prof["max_sector_pct"],
                  "교차분할": ("없음" if _mod <= 1 else
                           (f"{_mod}등분 중 {int(_sp.get('rem', 0)) + 1}번째"
                            if _shared is None
                            else f"공용 {_shared:.0%} + 전용 "
                                 f"{1 - _shared:.0%}")),
                  "분할후종목풀": log_n,
                  "팩터가중치": list(prof["qvm_weights"] or config.QVM_WEIGHTS)}
    return {**_prof_info, "selected": len(invest), "buy_signals": n_buy, "sell_signals": n_sell,
            "coverage": coverage, "유동성": liq, "선정": sel,
            "excel": str(xlsx_path), "model_date": today,
            "top_buys": top_buys.to_dict(orient="records")}


if __name__ == "__main__":
    r = run()
    print({k: v for k, v in r.items() if k != "top_buys"})
