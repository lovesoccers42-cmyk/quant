# -*- coding: utf-8 -*-
"""한국·미국 공통 QVM 계산.

두 시장이 같은 정의를 쓰도록 계산부를 한곳에 모았습니다.
factor-Global 노트북에 들어 있던 개선점을 기준으로 통일했습니다.

  1. 극단값을 버리지(NaN) 않고 경계값으로 눌러(winsorize) 종목 손실을 막습니다.
  2. 모멘텀은 12M-1M — 최근 약 1개월을 빼 단기 반전 효과를 제거합니다.
  3. TTM은 4개 분기 간격이 400일을 넘으면 무효로 봅니다(분기 누락 방어).
  4. K_ratio는 관측치 60개 미만이면 계산하지 않습니다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import zscore

SPAN_LIMIT_DAYS = 400   # 4개 분기(=diff(3))가 이보다 길면 TTM 무효
MOM_SKIP_DAYS = 21      # 모멘텀에서 제외할 최근 거래일 수 (약 1개월)
K_RATIO_MIN_OBS = 60    # K_ratio 최소 관측치


def latest_ttm(fs: pd.DataFrame, *, symbol: str, account: str, date: str,
               value: str, mean_accounts: tuple = ()) -> pd.DataFrame:
    """종목·계정별 최근 TTM 한 줄씩. mean_accounts는 합이 아닌 평균(÷4)으로."""
    fs = fs.copy()
    fs[date] = pd.to_datetime(fs[date])
    fs = fs.sort_values([symbol, account, date])

    fs["ttm"] = (fs.groupby([symbol, account])[value]
                 .transform(lambda s: s.rolling(window=4, min_periods=4).sum()))

    # 분기 연속성: 4개 분기가 400일을 넘게 벌어져 있으면 TTM을 믿을 수 없음
    span = fs.groupby([symbol, account])[date].diff(3).dt.days
    fs.loc[span > SPAN_LIMIT_DAYS, "ttm"] = np.nan

    if mean_accounts:
        fs["ttm"] = np.where(fs[account].isin(list(mean_accounts)),
                             fs["ttm"] / 4, fs["ttm"])

    return fs.groupby([symbol, account]).tail(1)


def safe_z(s: pd.Series) -> pd.Series:
    """표준편차가 0이거나 유효값이 1개뿐이면 0으로 돌려주는 z점수.

    scipy의 zscore는 분산이 0이면 0/0 → 전부 NaN을 내놓습니다. 팩터 합산은
    skipna=False라 한 컬럼만 NaN이 돼도 그 종목의 점수가 통째로 사라집니다.
    실제로 배당 데이터가 전부 비어 DY가 모두 0이 되자, 그 한 컬럼 때문에
    z_value가 전 종목 NaN이 되고 선정 종목이 0개가 된 적이 있습니다.
    """
    s = pd.to_numeric(s, errors="coerce")
    valid = s.dropna()
    if len(valid) < 2 or float(valid.std(ddof=0)) == 0:
        return pd.Series(np.where(s.notna(), 0.0, np.nan),
                         index=s.index, dtype="float64")
    return pd.Series(zscore(s, nan_policy="omit"), index=s.index, dtype="float64")


def col_clean(df: pd.DataFrame, cutoff: float = 0.01, asc: bool = False) -> pd.DataFrame:
    """상하위 cutoff를 경계값으로 누른 뒤 순위 → z점수."""
    q_low = df.quantile(cutoff)
    q_hi = df.quantile(1 - cutoff)
    df_trim = df.clip(q_low, q_hi, axis=1)
    return df_trim.rank(axis=0, ascending=asc).apply(safe_z)


def k_ratio(ret_cum: pd.DataFrame, min_obs: int = K_RATIO_MIN_OBS) -> pd.Series:
    """누적 로그수익률에 대한 절편 없는 OLS의 기울기 / 표준오차.

    statsmodels sm.OLS(y, x).fit()의 params[0]/bse[0]과 같으며,
    수천 종목을 한 번에 처리하기 위해 닫힌 형태로 계산합니다.
    """
    # 컬럼마다 pandas로 접근하면 종목 수 × 리밸런싱 횟수만큼 오버헤드가 쌓입니다.
    # (일간 리밸런싱이면 2,600종목 × 740회) 한 번에 numpy로 내려서 처리합니다.
    arr = ret_cum.to_numpy(dtype="float64")
    out = {}
    for j, col in enumerate(ret_cum.columns):
        a = arr[:, j]
        y = a[~np.isnan(a)]
        n = len(y)
        if n < min_obs:
            out[col] = np.nan
            continue
        x = np.arange(n, dtype="float64")
        sxx = float((x * x).sum())
        if sxx == 0:
            out[col] = np.nan
            continue
        beta = float((x * y).sum()) / sxx
        rss = float(((y - beta * x) ** 2).sum())
        if n - 1 <= 0 or rss <= 0:
            out[col] = np.nan
            continue
        se = np.sqrt(rss / (n - 1) / sxx)
        out[col] = beta / se if se > 0 else np.nan
    return pd.Series(out, dtype="float64")


def momentum(price_pivot: pd.DataFrame, skip_days: int = MOM_SKIP_DAYS):
    """(12M-1M 수익률 DataFrame, K_ratio Series) 반환."""
    price_pivot = price_pivot.sort_index()

    # 최근 skip_days를 제외한 지점까지의 수익률 (단기 반전 제거)
    end_pos = -skip_days if len(price_pivot) > skip_days + 1 else -1
    ret_list = pd.DataFrame(
        data=(price_pivot.ffill().iloc[end_pos] / price_pivot.bfill().iloc[0]) - 1,
        columns=["12M"])

    ret = price_pivot.pct_change().iloc[1:]
    ret_cum = np.log(1 + ret).cumsum()
    return ret_list, k_ratio(ret_cum)


def liquidity_eligible(price: pd.DataFrame, *, symbol: str, date: str,
                       close: str, volume: str, asof=None,
                       window: int = 20, min_value: float = 0.0):
    """최근 window 거래일 평균 거래대금이 min_value 이상인 종목 집합.

    '실제로 살 수 있는가'를 거릅니다. 거래대금이 하루 몇천만원인 종목은
    모델이 아무리 좋게 봐도 원하는 수량을 못 삽니다.
    min_value가 0이면 필터를 끕니다(None 반환).
    """
    if min_value <= 0 or volume not in price.columns:
        return None, {"적용": False}

    df = price if asof is None else price[price[date] <= asof]
    if df.empty:
        return None, {"적용": False, "사유": "주가 데이터 없음"}

    df = df.sort_values(date).groupby(symbol, observed=True).tail(window)
    value = pd.to_numeric(df[close], errors="coerce") * pd.to_numeric(df[volume],
                                                                     errors="coerce")
    avg = value.groupby(df[symbol]).mean().dropna()
    eligible = set(avg[avg >= min_value].index)

    return eligible, {"적용": True, "기준_거래대금": min_value,
                      "검사종목": int(len(avg)), "통과": len(eligible),
                      "탈락": int(len(avg) - len(eligible))}


def select_portfolio(scored: pd.DataFrame, *, symbol: str, sector: str,
                     n: int, max_sector_pct: float = 1.0, eligible=None):
    """qvm 상위에서 포트폴리오를 고릅니다.

    max_sector_pct: 한 섹터가 차지할 수 있는 최대 비중 (1.0이면 제한 없음).

    섹터 중립 z점수는 '섹터 안에서의 우열'만 맞춥니다. 최종 qvm은 전 종목을
    한 줄로 세우기 때문에, 그 시기에 좋았던 섹터가 통째로 상위를 차지합니다.
    실제로 한국 백테스트에서 IT가 보유의 44%를 먹었습니다. 그건 분산된
    포트폴리오가 아니라 사실상 그 섹터에 베팅한 것입니다.
    """
    df = scored.dropna(subset=["qvm"]).sort_values("qvm").copy()
    stats = {"후보": len(df)}

    if eligible is not None:
        before = len(df)
        df = df[df[symbol].astype(str).isin(eligible)]
        stats["유동성탈락"] = before - len(df)

    if 0 < max_sector_pct < 1 and len(df):
        want = max(1, int(np.ceil(n * max_sector_pct)))
        rank_in_sector = df.groupby(sector, observed=True).cumcount()

        # 섹터 수가 적으면 요청한 상한으로는 n종목을 못 채웁니다.
        # (예: 섹터 3개 · 상한 25% → 최대 3 × ceil(n/4)). 그럴 때 조용히
        # 종목 수를 줄이는 대신 상한을 필요한 만큼만 올리고 그 사실을 남깁니다.
        cap = want
        while cap < n and int((rank_in_sector < cap).sum()) < n:
            cap += 1

        capped = df[rank_in_sector < cap]
        stats["섹터상한탈락"] = len(df) - len(capped)
        stats["섹터별상한"] = cap
        if cap != want:
            stats["상한완화"] = (f"{want}종목 → {cap}종목 "
                              f"(섹터가 {df[sector].nunique()}개뿐이라 "
                              f"요청한 {max_sector_pct:.0%}로는 {n}종목을 못 채움)")
        df = capped

    out = df.head(n).copy()
    out.insert(0, "순위", range(1, len(out) + 1))

    if len(out):
        top = out[sector].value_counts()
        stats["선정"] = len(out)
        stats["최대섹터"] = f"{top.index[0]} {top.iloc[0]}종목 ({top.iloc[0]/len(out):.0%})"
        stats["섹터수"] = int(top.size)
    return out, stats


def build_scores(data_bind: pd.DataFrame, *, symbol: str, sector: str,
                 weights, n_portfolio: int) -> pd.DataFrame:
    """z_quality / z_value / z_momentum / qvm / invest 컬럼을 붙여 반환.

    data_bind에는 ROE·GPA·CFO, PBR·PCR·PER·PSR·DY, 12M·K_ratio가 있어야 합니다.
    """
    data_bind = data_bind.copy()

    # 무배당 종목이 밸류 팩터에서 통째로 빠지지 않도록 DY 결측은 0
    if "DY" in data_bind.columns:
        data_bind["DY"] = data_bind["DY"].fillna(0)

    group = data_bind.set_index([symbol, sector]).groupby(sector)

    # 퀄리티 — 높을수록 좋음
    z_quality = (group[["ROE", "GPA", "CFO"]]
                 .apply(lambda x: col_clean(x, 0.01, False))
                 .sum(axis=1, skipna=False).to_frame("z_quality")
                 .droplevel(axis=0, level=0))
    data_bind = data_bind.merge(z_quality, how="left", on=[symbol, sector])

    # 밸류 — 낮을수록 좋음, DY만 높을수록 좋음
    v1 = (group[["PBR", "PCR", "PER", "PSR"]]
          .apply(lambda x: col_clean(x, 0.01, True)).droplevel(axis=0, level=0))
    v2 = (group[["DY"]]
          .apply(lambda x: col_clean(x, 0.01, False)).droplevel(axis=0, level=0))
    z_value = (v1.merge(v2, on=[symbol, sector])
               .sum(axis=1, skipna=False).to_frame("z_value"))
    data_bind = data_bind.merge(z_value, how="left", on=[symbol, sector])

    # 모멘텀 — 높을수록 좋음
    z_mom = (group[["12M", "K_ratio"]]
             .apply(lambda x: col_clean(x, 0.01, False))
             .sum(axis=1, skipna=False).to_frame("z_momentum")
             .droplevel(axis=0, level=0))
    data_bind = data_bind.merge(z_mom, how="left", on=[symbol, sector])

    # 최종 QVM — 낮을수록 우수
    final = (data_bind[[symbol, "z_quality", "z_value", "z_momentum"]]
             .set_index(symbol).apply(safe_z))
    final.columns = ["quality", "value", "momentum"]
    qvm = (final * list(weights)).sum(axis=1, skipna=False).to_frame("qvm")

    port = data_bind.merge(qvm, on=symbol)
    port["invest"] = np.where(port["qvm"].rank() <= n_portfolio, "Y", "N")
    return port
