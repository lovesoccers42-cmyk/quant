# -*- coding: utf-8 -*-
"""QVM 백테스트 엔진.

과거 시점으로 되돌려 리밸런싱을 반복하고, 실제로 시장을 이겼는지 잽니다.
점수 계산은 실전과 같은 factor_core를 그대로 씁니다 — 백테스트와 실전이
다른 코드를 쓰면 결과를 믿을 수 없기 때문입니다.

미리 보기(look-ahead) 차단:
  · 재무제표는 기준일 + 공시지연(기본 90일)이 지난 것만 씁니다.
    2026-03-31 분기 실적은 2026-06-29 이후 리밸런싱부터 보입니다.
  · 주가·모멘텀은 리밸런싱 당일까지만 씁니다.

정직하게 밝히는 한계 (요약 리포트에도 그대로 실립니다):
  1. 생존편향 — 저장소에 현재 상장 종목만 있습니다. 그 사이 상장폐지된
     종목의 손실이 통째로 빠져 성과가 과대평가됩니다. 가장 큰 한계입니다.
  2. 시가총액 근사 — 과거 시총을 '그날 종가 × 현재 주식수'로 봅니다.
     증자·감자·액면분할이 반영되지 않습니다.
  3. 섹터·배당은 현재 값을 과거에도 적용합니다.
  4. 거래비용은 가정치입니다(기본 편도 25bp).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
import factor_core as fc
import store

log = logging.getLogger("quant_agent.backtest")

TRADING_DAYS = 252


@dataclass(frozen=True)
class Spec:
    """시장별 테이블·컬럼 이름."""
    label: str
    price: str; p_date: str; p_close: str; p_sym: str; p_vol: str
    fs: str; f_date: str; f_acct: str; f_val: str; f_freq: str
    ticker: str; t_date: str; t_sym: str; t_name: str; t_mcap: str; t_close: str
    mcap_scale: float          # 시총을 재무제표 단위로 맞추는 나눗셈
    dps: str | None            # 주당배당금 컬럼 (없으면 DY 생략)
    sector_tbl: str | None; sector_key: str | None
    sector_col: str
    quality: dict              # 지표 → (분자 계정, 분모 계정)
    value: dict                # 지표 → 계정
    mean_accounts: tuple
    common_only: bool


SPECS = {
    "kr": Spec(
        label="한국",
        price="kor_price", p_date="날짜", p_close="종가", p_sym="종목코드",
        p_vol="거래량",
        fs="kor_fs", f_date="기준일", f_acct="계정", f_val="값", f_freq="공시구분",
        ticker="kor_ticker", t_date="기준일", t_sym="종목코드", t_name="종목명",
        t_mcap="시가총액", t_close="종가",
        mcap_scale=1e8,                       # 원 → 억원 (kor_fs가 억원 단위)
        dps="주당배당금",
        sector_tbl="kor_sector", sector_key="CMP_CD", sector_col="SEC_NM_KOR",
        quality={"ROE": ("당기순이익", "자본"), "GPA": ("매출총이익", "자산"),
                 "CFO": ("영업활동으로인한현금흐름", "자산")},
        value={"PER": "당기순이익", "PBR": "자본",
               "PCR": "영업활동으로인한현금흐름", "PSR": "매출액"},
        mean_accounts=("자산", "자본"),
        common_only=True,
    ),
    "us": Spec(
        label="미국",
        price="global_price", p_date="Date", p_close="Close", p_sym="Symbol",
        p_vol="Volume",
        fs="global_fs", f_date="date", f_acct="account", f_val="value", f_freq="freq",
        ticker="global_ticker", t_date="date", t_sym="Symbol", t_name="Name",
        t_mcap="Market Cap", t_close=None,
        mcap_scale=1.0,
        dps=None,
        sector_tbl=None, sector_key=None, sector_col="Sector",
        quality={"ROE": ("NetIncome", "StockholdersEquity"),
                 "GPA": ("GrossProfit", "TotalAssets"),
                 "CFO": ("CashFlowFromContinuingOperatingActivities", "TotalAssets")},
        value={"PER": "NetIncome", "PBR": "StockholdersEquity",
               "PCR": "CashFlowFromContinuingOperatingActivities",
               "PSR": "TotalRevenue"},
        mean_accounts=("TotalAssets", "StockholdersEquity"),
        common_only=False,
    ),
}


# ── 데이터 적재 ──────────────────────────────────────────────
def _load(spec: Spec):
    accounts = sorted({a for pair in spec.quality.values() for a in pair}
                      | set(spec.value.values()))
    acct_sql = ", ".join(f"'{a}'" for a in accounts)

    price = store.read_sql(
        f'select "{spec.p_date}", "{spec.p_close}", "{spec.p_vol}", "{spec.p_sym}" from {spec.price};')
    fs = store.read_sql(
        f"select * from {spec.fs} where {spec.f_freq} = 'q' "
        f"and {spec.f_acct} in ({acct_sql});")
    ticker = store.read_sql(
        f"select * from {spec.ticker} "
        f"where {spec.t_date} = (select max({spec.t_date}) from {spec.ticker});")

    if spec.common_only and "종목구분" in ticker.columns:
        ticker = ticker[ticker["종목구분"] == "보통주"]

    sector = None
    if spec.sector_tbl:
        sector = store.read_sql(
            f"select * from {spec.sector_tbl} "
            f"where 기준일 = (select max(기준일) from {spec.sector_tbl});")

    return price, fs, ticker, sector


def _shares(ticker: pd.DataFrame, spec: Spec) -> pd.Series:
    """현재 시총 ÷ 현재 종가 = 주식수(근사). 과거 시총 추정에 씁니다."""
    if spec.t_close is None or spec.t_close not in ticker.columns:
        return pd.Series(dtype="float64")
    close = pd.to_numeric(ticker[spec.t_close], errors="coerce")
    mcap = pd.to_numeric(ticker[spec.t_mcap], errors="coerce")
    sh = (mcap / close).replace([np.inf, -np.inf], np.nan)
    return pd.Series(sh.values, index=ticker[spec.t_sym].values)


# ── 시점별 점수 ──────────────────────────────────────────────
def _ttm_pivot(fs, cutoff, spec, cache):
    """공시지연을 반영한 TTM 피벗. 분기마다만 바뀌므로 캐시합니다.

    일간 리밸런싱이면 1,000번 넘게 불리는데 실제 내용은 분기마다만
    바뀝니다. '그 시점에 알려진 마지막 분기'를 키로 캐시하면
    같은 계산을 수백 번 반복하지 않습니다.
    """
    known = fs[spec.f_date] <= cutoff
    if not known.any():
        return None
    key = fs.loc[known, spec.f_date].max()
    if key in cache:
        return cache[key]

    ttm = fc.latest_ttm(fs[known], symbol=spec.p_sym if spec.p_sym in fs.columns
                        else spec.t_sym,
                        account=spec.f_acct, date=spec.f_date, value=spec.f_val,
                        mean_accounts=spec.mean_accounts)
    sym_col = spec.p_sym if spec.p_sym in ttm.columns else spec.t_sym
    pivot = ttm.pivot(index=sym_col, columns=spec.f_acct, values="ttm")

    for metric, (num, den) in spec.quality.items():
        if num in pivot.columns and den in pivot.columns:
            pivot[metric] = pivot[num] / pivot[den]
        else:
            pivot[metric] = np.nan

    cache[key] = (pivot, sym_col)
    return cache[key]


def _score_at(asof, price_pivot, fs, ticker, sector, shares, spec, lag_days,
              ttm_cache=None):
    """asof 시점에 실제로 알 수 있었던 정보만으로 QVM 점수를 냅니다."""
    got = _ttm_pivot(fs, asof - pd.Timedelta(days=lag_days), spec,
                     ttm_cache if ttm_cache is not None else {})
    if got is None:
        return None
    pivot, sym_col = got
    pivot = pivot.copy()

    # 그날의 주가와 추정 시총
    px = price_pivot.loc[:asof]
    if len(px) < 60:
        return None
    last_px = px.ffill().iloc[-1]

    if len(shares):
        mcap = (last_px * shares.reindex(last_px.index)) / spec.mcap_scale
    else:   # 주식수를 못 구하면 현재 시총을 그대로 (정확도 낮음)
        mcap = pd.Series(
            pd.to_numeric(ticker.set_index(spec.t_sym)[spec.t_mcap], errors="coerce")
            / spec.mcap_scale)
        mcap = mcap.reindex(last_px.index)

    # 밸류
    for metric, acct in spec.value.items():
        if acct in pivot.columns:
            v = mcap.reindex(pivot.index) / pivot[acct]
            pivot[metric] = v.replace([np.inf, -np.inf], np.nan)
        else:
            pivot[metric] = np.nan
        pivot.loc[pivot[metric] <= 0, metric] = np.nan

    # 배당수익률 (현재 주당배당금 ÷ 그날 종가 — 근사)
    if spec.dps and spec.dps in ticker.columns:
        dps = pd.Series(pd.to_numeric(ticker.set_index(spec.t_sym)[spec.dps],
                                      errors="coerce"))
        dy = (dps.reindex(last_px.index) / last_px).replace([np.inf, -np.inf], np.nan)
        pivot["DY"] = dy.reindex(pivot.index)
    else:
        pivot["DY"] = np.nan

    # 모멘텀 (그날까지의 주가만)
    ret_list, k = fc.momentum(px)
    pivot["12M"] = ret_list["12M"].reindex(pivot.index)
    pivot["K_ratio"] = k.reindex(pivot.index)

    # 섹터 붙이기
    base = ticker[[spec.t_sym, spec.t_name]].copy()
    base.columns = ["symbol", "name"]
    if sector is not None:
        smap = dict(zip(sector[spec.sector_key].astype(str),
                        sector[spec.sector_col].astype(str)))
        base["sector"] = base["symbol"].astype(str).map(smap)
    else:
        base["sector"] = base["symbol"].astype(str).map(
            dict(zip(ticker[spec.t_sym].astype(str),
                     ticker[spec.sector_col].astype(str))))
    base["sector"] = base["sector"].fillna("기타")

    cols = ["ROE", "GPA", "CFO", "PER", "PBR", "PCR", "PSR", "DY", "12M", "K_ratio"]
    merged = base.merge(pivot[cols].reset_index().rename(columns={sym_col: "symbol"}),
                        on="symbol", how="inner")
    merged = merged.drop_duplicates("symbol")
    if len(merged) < 30:
        return None

    scored = fc.build_scores(merged, symbol="symbol", sector="sector",
                             weights=config.QVM_WEIGHTS, n_portfolio=len(merged))
    return scored.dropna(subset=["qvm"])


# ── 본체 ─────────────────────────────────────────────────────
def run(market: str = "kr", top_n: int = 30, rebalance: str = "ME",
        cost_bps: float = 25.0, lag_days: int = 90,
        max_sector_pct: float = 1.0, min_turnover: float = 0.0) -> dict:
    spec = SPECS[market]
    price, fs, ticker, sector = _load(spec)

    if price.empty or fs.empty or ticker.empty:
        raise RuntimeError(f"{spec.label}장 백테스트에 필요한 데이터가 없습니다 "
                           f"(주가 {len(price)}행, 재무 {len(fs)}행, 종목 {len(ticker)}행)")

    price[spec.p_date] = pd.to_datetime(price[spec.p_date])
    fs[spec.f_date] = pd.to_datetime(fs[spec.f_date])

    universe = set(ticker[spec.t_sym].astype(str))
    price = price[price[spec.p_sym].astype(str).isin(universe)]
    price_pivot = price.pivot_table(index=spec.p_date, columns=spec.p_sym,
                                    values=spec.p_close, aggfunc="last").sort_index()

    shares = _shares(ticker, spec)

    # 거래대금 피벗을 미리 한 번만 만듭니다. 날짜마다 전체를 정렬하면
    # 일간 리밸런싱에서 같은 정렬을 1,000번 반복하게 됩니다.
    turnover_pivot = None
    if min_turnover > 0 and spec.p_vol in price.columns:
        tv = price.assign(_v=pd.to_numeric(price[spec.p_close], errors='coerce')
                          * pd.to_numeric(price[spec.p_vol], errors='coerce'))
        turnover_pivot = tv.pivot_table(index=spec.p_date, columns=spec.p_sym,
                                        values='_v', aggfunc='last').sort_index()

    # 리밸런싱 날짜 — 모멘텀에 1년이 필요하므로 첫 1년은 건너뜁니다
    first = price_pivot.index.min() + pd.DateOffset(years=1)
    dates = [d for d in
             pd.date_range(price_pivot.index.min(), price_pivot.index.max(),
                           freq=rebalance)
             if d >= first]
    dates = [price_pivot.index[price_pivot.index <= d].max() for d in dates]
    dates = sorted({d for d in dates if pd.notna(d)})

    if len(dates) < 3:
        raise RuntimeError(
            f"리밸런싱 시점이 {len(dates)}개뿐이라 백테스트가 의미 없습니다. "
            f"보관 주가가 {config.PRICE_KEEP_YEARS}년인데 모멘텀에 1년을 쓰므로 "
            f"실제 검증 구간은 그만큼 짧아집니다. "
            f"QUANT_PRICE_KEEP_YEARS를 늘려 더 긴 주가를 모으세요.")

    log.info("%s장 백테스트: %s ~ %s, 리밸런싱 %d회, 상위 %d종목",
             spec.label, dates[0].date(), dates[-1].date(), len(dates), top_n)

    rows, holdings_log, sel_log = [], [], []
    ttm_cache = {}
    prev_holdings: set = set()

    for i, t in enumerate(dates[:-1]):
        t_next = dates[i + 1]
        scored = _score_at(t, price_pivot, fs, ticker, sector, shares, spec,
                           lag_days, ttm_cache)
        if scored is None or scored.empty:
            continue

        eligible = None
        if turnover_pivot is not None:
            win = turnover_pivot.loc[:t].tail(config.LIQUIDITY_WINDOW)
            avg = win.mean()
            eligible = set(avg[avg >= min_turnover].index)

        picks, sel = fc.select_portfolio(
            scored, symbol="symbol", sector="sector", n=top_n,
            max_sector_pct=max_sector_pct, eligible=eligible)
        sel_log.append({"리밸런싱일": t, **sel})

        codes = [c for c in picks["symbol"] if c in price_pivot.columns]
        if len(codes) < 5:
            continue

        window = price_pivot.loc[t:t_next, codes].ffill()
        if len(window) < 2:
            continue
        rets = (window.iloc[-1] / window.iloc[0] - 1).dropna()
        if rets.empty:
            continue
        port_ret = float(rets.mean())

        # 벤치마크 — 같은 기간 전 종목 동일가중
        bwin = price_pivot.loc[t:t_next].ffill()
        brets = (bwin.iloc[-1] / bwin.iloc[0] - 1).dropna()
        bench_ret = float(brets.mean()) if len(brets) else 0.0

        held = set(codes)
        turnover = (len(held - prev_holdings) / max(len(held), 1)) if prev_holdings else 1.0
        cost = turnover * (cost_bps / 10000) * 2      # 팔고 사는 왕복
        prev_holdings = held

        rows.append({"리밸런싱일": t, "다음리밸런싱": t_next,
                     "종목수": len(codes), "수익률": port_ret,
                     "비용차감수익률": port_ret - cost,
                     "벤치마크": bench_ret, "턴오버": turnover})

        top = picks.head(min(top_n, len(picks)))
        holdings_log.append(pd.DataFrame({
            "리밸런싱일": t, "종목코드": top["symbol"].values,
            "종목명": top["name"].values, "섹터": top["sector"].values,
            "qvm": top["qvm"].round(4).values}))

    if not rows:
        raise RuntimeError("유효한 리밸런싱이 한 번도 없었습니다.")

    perf = pd.DataFrame(rows)
    perf["누적"] = (1 + perf["비용차감수익률"]).cumprod()
    perf["누적_벤치마크"] = (1 + perf["벤치마크"]).cumprod()

    summary = _metrics(perf, spec, top_n, cost_bps, lag_days)
    summary["제약"] = {
        "섹터상한": ("없음" if max_sector_pct >= 1
                  else f"한 섹터 최대 {max_sector_pct:.0%}"),
        "유동성기준": ("없음" if min_turnover <= 0
                   else f"{config.LIQUIDITY_WINDOW}일 평균 거래대금 {min_turnover:,.0f} 이상"),
    }
    if sel_log:
        sl = pd.DataFrame(sel_log)
        for k in ("유동성탈락", "섹터상한탈락"):
            if k in sl.columns:
                summary["제약"][f"평균 {k}"] = int(sl[k].mean())
    holdings = pd.concat(holdings_log, ignore_index=True) if holdings_log else pd.DataFrame()

    return {"market": market, "summary": summary, "perf": perf, "holdings": holdings}


def _metrics(perf: pd.DataFrame, spec: Spec, top_n: int,
             cost_bps: float, lag_days: int) -> dict:
    days = (perf["다음리밸런싱"].iloc[-1] - perf["리밸런싱일"].iloc[0]).days
    years = max(days / 365.25, 1e-9)

    def stats(cum: pd.Series, per: pd.Series) -> dict:
        total = float(cum.iloc[-1]) - 1
        cagr = (1 + total) ** (1 / years) - 1 if total > -1 else float("nan")
        periods_per_year = len(per) / years
        vol = float(per.std(ddof=1)) * np.sqrt(periods_per_year) if len(per) > 1 else np.nan
        sharpe = (cagr / vol) if (vol and vol == vol and vol > 0) else float("nan")
        dd = cum / cum.cummax() - 1
        return {"누적수익률": round(total * 100, 2),
                "연환산수익률": round(cagr * 100, 2),
                "연변동성": round(vol * 100, 2) if vol == vol else None,
                "샤프": round(sharpe, 2) if sharpe == sharpe else None,
                "최대낙폭": round(float(dd.min()) * 100, 2),
                "승률": round(float((per > 0).mean()) * 100, 1)}

    port = stats(perf["누적"], perf["비용차감수익률"])
    bench = stats(perf["누적_벤치마크"], perf["벤치마크"])

    return {
        "시장": spec.label,
        "기간": f"{perf['리밸런싱일'].iloc[0]:%Y-%m-%d} ~ {perf['다음리밸런싱'].iloc[-1]:%Y-%m-%d}",
        "리밸런싱횟수": int(len(perf)),
        "보유종목수": top_n,
        "평균턴오버": round(float(perf["턴오버"].mean()) * 100, 1),
        "거래비용가정": f"편도 {cost_bps:.0f}bp",
        "공시지연가정": f"{lag_days}일",
        "포트폴리오": port,
        "벤치마크(전종목 동일가중)": bench,
        "초과수익률": round(port["누적수익률"] - bench["누적수익률"], 2),
        "한계": [
            "생존편향: 상장폐지 종목이 데이터에 없어 성과가 과대평가됩니다 (가장 큰 한계)",
            "과거 시가총액은 '그날 종가 × 현재 주식수' 근사 — 증자·감자·분할 미반영",
            "섹터와 주당배당금은 현재 값을 과거에도 적용",
            "과거 성과가 미래 수익을 보장하지 않습니다",
        ],
    }
