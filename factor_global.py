# -*- coding: utf-8 -*-
"""미국장 QVM 팩터 모델 → 엑셀 + parquet 출력.

노트북(factor-Global)과 같이 기술적 매매 신호 없이 QVM 순위만 냅니다.
계산 정의는 한국장과 공유합니다(factor_core).

2026-10: 실전 운용용으로 factor_kor와 같은 모양을 갖췄습니다.
  · 프로필(상수/배우자)별 산출 — 교차 분할 포함
  · 주문 엔진이 읽는 parquet은 상위 100이 아니라 MODEL_ROWS만큼 깊게
    (1주 값이 배정액보다 비싼 종목이 빠지면 다음 순위로 채워야 합니다.
     미국은 주가가 $6~$780으로 흩어져 있어 한국보다 훨씬 자주 걸립니다)
  · 유지 판정용 넓은 명단(hold) 파일 — 순위 버퍼
  · 종가 컬럼 — 몇 주 살지 계산하려면 필요합니다

**컬럼 이름에 대하여.** 데이터 계층은 Symbol/Name/Sector로 말하고 주문·
상태 계층(portfolio.py)은 종목코드/종목명/SEC_NM_KOR/종가로 말합니다.
경계인 이 파일에서 둘 다 실어 보냅니다 — 주문 엔진을 미국용으로 또
갈라 쓰면 한국에서 고친 버그를 미국에서 다시 만나게 됩니다.
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
        select Date, Close, Volume, Symbol from global_price
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


def _alias(df: pd.DataFrame) -> pd.DataFrame:
    """주문 엔진이 읽는 이름을 나란히 붙입니다 (값은 같은 열을 가리킵니다)."""
    out = df.copy()
    out["종목코드"] = out["Symbol"].astype(str).str.strip().str.upper()
    out["종목명"] = out["Name"] if "Name" in out.columns else ""
    out["SEC_NM_KOR"] = out["Sector"] if "Sector" in out.columns else "Unknown"
    return out


def run(profile: str | None = None) -> dict:
    """프로필별로 점수와 명단을 만듭니다 (미국).

    프로필이 다르면 교차 분할 소속이 달라져 선정 종목이 달라집니다.
    산출 파일 이름도 프로필마다 다릅니다 — 섞이면 한쪽 명단으로 다른 쪽
    주문서를 내게 됩니다.
    """
    prof = config.profile(profile, market="us")
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

    # 종가 — 주문서가 '몇 주 살지'를 계산하는 데 씁니다. 미국도 1주 단위라
    # 배정액을 주가로 나눠야 수량이 나옵니다 (소수점 매매는 쓰지 않습니다).
    _pf = price_pivot.ffill()
    last_px = _pf.iloc[-1]
    data_bind["종가"] = data_bind["Symbol"].map(last_px)

    # 변동성 (저변동성 팩터용)
    _dr = _pf.tail(config.VOL_WINDOW + 1).pct_change(fill_method=None)
    data_bind["VOL"] = data_bind["Symbol"].map(_dr.std(ddof=1))

    port = fc.build_scores(
        data_bind, symbol="Symbol", sector="Sector",
        weights=(prof["qvm_weights"] or
                 (config.QVML_WEIGHTS if config.USE_LOWVOL
                  else config.QVM_WEIGHTS)),
        n_portfolio=len(data_bind), neutral=prof["sector_neutral"])

    coverage = {k: int(port[k].notna().sum())
                for k in ("ROE", "PER", "12M", "K_ratio",
                          "z_quality", "z_value", "z_momentum", "qvm")}

    eligible, liq = fc.liquidity_eligible(
        price_list, symbol="Symbol", date="Date", close="Close", volume="Volume",
        window=config.LIQUIDITY_WINDOW, min_value=config.MIN_TURNOVER_US)

    # 점수가중은 '전체 유니버스 순위'로 비중을 매깁니다 (백테스트와 동일).
    port = port.copy()
    port["전체순위"] = port["qvm"].rank(method="first")
    port["전체종목수"] = int(port["qvm"].notna().sum())

    # 교차 분할 — 티커 해시로 고정 배정. 한국과 같은 규칙(factor_core)이라
    # 두 계좌의 전용 종목이 영구히 고정됩니다.
    _sp = prof["split"] or {}
    _mod = max(1, int(_sp.get("mod", 1) or 1))
    _shared = _sp.get("shared")
    _shared = None if _shared is None else min(1.0, max(0.0, float(_shared)))
    if _shared is not None and _shared >= 1.0:
        _mod = 1
    if _mod > 1:
        _rem = int(_sp.get("rem", 0) or 0) % _mod
        if _shared is None:
            _mine = port["Symbol"].astype(str).map(
                lambda c: fc.split_bucket(c, _mod) == _rem)
        else:
            _mine = port["Symbol"].astype(str).map(
                lambda c: fc.split_keep(c, _shared, _rem, _mod))
        port = port[_mine]
    log_n = len(port)

    invest, sel = fc.select_portfolio(
        port, symbol="Symbol", sector="Sector", n=config.N_PORTFOLIO,
        max_sector_pct=prof["max_sector_pct"], eligible=eligible)

    # 주문 엔진에 넘기는 파일은 상위 100종목보다 깊게 씁니다 — 가격 때문에
    # 빠진 자리를 다음 순위로 채워야 하기 때문입니다. 미국 주가 분포
    # (유동성 통과 2,548종목 실측: 중앙 $40 · 90분위 $229)에서는 자본이
    # 작을수록 이 구멍이 커집니다.
    wide, _ = fc.select_portfolio(
        port, symbol="Symbol", sector="Sector",
        n=max(config.MODEL_ROWS, config.N_PORTFOLIO),
        max_sector_pct=prof["max_sector_pct"], eligible=eligible)

    # 유지 판정용 넓은 명단 (순위 버퍼). 살 때는 상위 100위에서만 고르고,
    # 팔 때는 이 명단 밖으로 밀려나야 팝니다.
    hold_n = int(round(config.N_PORTFOLIO * (1 + config.RANK_BUFFER)))
    hold_pool = port[port["Symbol"].astype(str).isin(eligible)] if eligible else port
    hold_list = _alias(hold_pool.dropna(subset=["qvm"]).nsmallest(hold_n, "qvm"))
    hold_list = (hold_list[["종목코드", "종목명", "SEC_NM_KOR", "qvm",
                            "Symbol", "Name", "Sector"]]
                 .reset_index(drop=True))
    hold_list.to_parquet(config.OUTPUT_DIR / prof["hold_file"], index=False)

    wide = _alias(wide).sort_values("qvm").reset_index(drop=True).round(4)
    invest = wide.head(config.N_PORTFOLIO).copy()

    today = f"{date.today():%Y%m%d}"
    _tag = "미국" if prof["key"] == "main" else f"미국-{prof['라벨']}"
    xlsx_path = config.OUTPUT_DIR / config.report_filename(
        _tag, today, token=f"model-us-{prof['key']}")
    invest.to_excel(xlsx_path, index=False)
    wide.to_parquet(config.OUTPUT_DIR / prof["model_file"], index=False)

    # 자본이 얼마나 필요한지 — 주문서를 내기 전에 알아야 하는 숫자입니다.
    _px = pd.to_numeric(invest["종가"], errors="coerce").dropna()
    _need = {}
    if len(_px):
        _tol = float(config.LOT_TOLERANCE)
        _need = {"상위100_주가중앙": round(float(_px.median()), 2),
                 "상위100_최고가": round(float(_px.max()), 2),
                 "전종목_1주씩_필요자본": round(float(_px.sum()), 0),
                 "가격제외_0%_되는_자본":
                     round(float(_px.max()) * config.N_PORTFOLIO / _tol, 0)}

    _prof_info = {"시장": "미국", "통화": prof["통화"],
                  "프로필": prof["key"], "라벨": prof["라벨"],
                  "섹터중립": prof["sector_neutral"],
                  "섹터상한": prof["max_sector_pct"],
                  "교차분할": ("없음" if _mod <= 1 else
                           (f"{_mod}등분 중 {int(_sp.get('rem', 0)) + 1}번째"
                            if _shared is None
                            else f"공용 {_shared:.0%} + 전용 "
                                 f"{1 - _shared:.0%}")),
                  "분할후종목풀": log_n,
                  "팩터가중치": list(prof["qvm_weights"] or config.QVM_WEIGHTS)}
    top = invest[["종목코드", "종목명", "SEC_NM_KOR", "종가", "qvm",
                  "z_quality", "z_value", "z_momentum"]].head(15)
    return {**_prof_info, "universe": len(port), "selected": len(invest),
            "model_rows": len(wide), "hold_rows": len(hold_list),
            "coverage": coverage, "유동성": liq, "선정": sel,
            "자본점검": _need,
            "excel": str(xlsx_path), "model_date": today,
            "top_buys": top.to_dict(orient="records")}


if __name__ == "__main__":
    import sys
    r = run(sys.argv[1] if len(sys.argv) > 1 else None)
    print({k: v for k, v in r.items() if k != "top_buys"})
