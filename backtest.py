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
from scipy.stats import spearmanr

import config
import factor_core as fc
import store

log = logging.getLogger("quant_agent.backtest")

TRADING_DAYS = 252

# 지수 벤치마크 종목 수 — 시총 상위 N종목 시총가중으로 코스피200을 근사합니다.
INDEX_N = 200


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


def _shares(ticker: pd.DataFrame, spec: Spec,
            price_pivot: pd.DataFrame | None = None) -> pd.Series:
    """시총 ÷ 종가 = 주식수(근사). 과거 시총 추정과 지수 벤치마크에 씁니다.

    종목표에 종가 컬럼이 없는 시장(미국)에서는 주가 피벗의 마지막 종가로
    대신합니다. 이게 없으면 shares가 비고, 그러면 지수 벤치마크가 조용히
    '전종목 동일가중'으로 떨어집니다 — 미국 런에서 두 벤치마크가 똑같이
    16.36%로 찍힌 게 그 증상이었습니다.
    """
    mcap = pd.to_numeric(ticker[spec.t_mcap], errors="coerce")
    sym = ticker[spec.t_sym].astype(str)

    close = None
    if spec.t_close is not None and spec.t_close in ticker.columns:
        close = pd.to_numeric(ticker[spec.t_close], errors="coerce")
        close.index = sym
    elif price_pivot is not None and len(price_pivot):
        last = price_pivot.ffill().iloc[-1]
        close = pd.Series(sym.map(last).values, index=sym)
    if close is None:
        return pd.Series(dtype="float64")

    mcap.index = sym
    sh = (mcap / close).replace([np.inf, -np.inf], np.nan)
    sh = sh[sh > 0]
    return sh[~sh.index.duplicated()]


# ── 종목별 비중 ──────────────────────────────────────────────
WEIGHTINGS = ("equal", "mcap", "score", "invvol")

# 방식별 한 종목 상한 기본값. 시총가중은 삼성전자 하나로 쏠리므로 8%,
# 점수·역변동성은 상위 쏠림이 덜해 3%로 둡니다. 동일가중은 상한이 무의미.
_DEFAULT_CAP = {"equal": 0.0, "mcap": 0.08, "score": 0.03, "invvol": 0.03}

# 상한은 '손보는 회차에 맞추는 값'입니다. 분할 리밸런싱에서는 안 건드리는
# 등분이 주가대로 흘러가므로, 많이 오른 종목은 다음 차례가 올 때까지 상한을
# 넘어 있을 수 있습니다. 그게 실제로 벌어지는 일이고 일부러 그렇게 뒀습니다.
# 다만 한 종목이 상한의 이 배수를 넘으면 차례를 기다리지 않고 바로 깎습니다.
# 상한을 둔 이유가 '한 종목 베팅을 막는 것'인데, 기다리는 동안 그게 깨지면
# 상한이 있으나 마나이기 때문입니다.
CAP_BREACH_MULT = 1.5


def _cap_weights(raw: pd.Series, cap: float) -> pd.Series:
    """합이 1이 되게 정규화하고, 한 종목이 cap을 넘으면 눌러 나머지에 비례 배분."""
    w = pd.to_numeric(raw, errors="coerce").astype(float).clip(lower=0.0)
    w = w.fillna(0.0)
    total = float(w.sum())
    if total <= 0 or len(w) == 0:
        return pd.Series(1.0 / max(len(w), 1), index=w.index, dtype="float64")
    w = w / total
    # cap × 종목수 < 1 이면 상한을 지키면서 100%를 채울 수 없습니다 → 동일가중
    if not (0 < cap < 1) or len(w) * cap < 1 - 1e-12:
        return w
    for _ in range(100):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = ~over
        base = float(w[free].sum())
        if base <= 0:
            w[free] = excess / max(int(free.sum()), 1)
            break
        w[free] = w[free] + excess * (w[free] / base)
    s = float(w.sum())
    return w / s if s > 0 else w


def _target_weights(codes, scheme: str, *, shares, price_pivot, t,
                    order, cap: float, vol_window: int) -> pd.Series:
    """이번 회차의 목표 비중. order는 그 시점 점수 순위(좋은 종목이 앞)."""
    idx = pd.Index([str(c) for c in codes])
    if scheme == "mcap" and len(shares):
        px = price_pivot.loc[:t].ffill().iloc[-1]
        raw = pd.to_numeric(px.reindex(idx), errors="coerce") * \
            pd.to_numeric(shares.reindex(idx), errors="coerce")
    elif scheme == "score":
        # 점수값 자체가 아니라 '순위'에 선형 가중을 줍니다. qvm은 z점수 합이라
        # 회차마다 척도가 달라져서, 값으로 비중을 매기면 비중이 시점마다 들쭉
        # 날쭉해집니다. 순위는 척도가 고정돼 있습니다.
        rank = {str(c): i for i, c in enumerate(order)}
        worst = len(order) + 1
        n = max(len(order), 1)
        raw = pd.Series([float(n - rank.get(c, worst) + 1) for c in idx],
                        index=idx, dtype="float64").clip(lower=1.0)
    elif scheme == "invvol":
        have = [c for c in idx if c in price_pivot.columns]
        dr = (price_pivot.loc[:t, have].tail(vol_window + 1).ffill()
              .pct_change(fill_method=None))
        sd = dr.std(ddof=1).reindex(idx)
        raw = (1.0 / sd).replace([np.inf, -np.inf], np.nan)
    else:
        raw = pd.Series(1.0, index=idx, dtype="float64")

    raw = pd.to_numeric(raw, errors="coerce")
    pos = raw[raw > 0]
    # 데이터가 절반도 안 채워지면 그 회차는 동일가중으로 물러섭니다.
    # 억지로 추정해 비중을 매기면 어느 회차가 추정값인지 알 수 없게 됩니다.
    if len(pos) < max(5, len(idx) // 2):
        raw = pd.Series(1.0, index=idx, dtype="float64")
    else:
        raw = raw.where(raw > 0).fillna(float(pos.median()))
    return _cap_weights(raw, cap)


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
              ttm_cache=None, neutral=None, weights=None, trend_stock=0):
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

    # 변동성 (저변동성 팩터용) — 최근 VOL_WINDOW 거래일 일간수익률 표준편차
    pxf = px.ffill()
    dr = pxf.tail(config.VOL_WINDOW + 1).pct_change(fill_method=None)
    pivot["VOL"] = dr.std(ddof=1).reindex(pivot.index)

    # 종목 단위 추세 — 자기 이동평균 위에 있는가 (미래 미사용)
    if trend_stock and trend_stock > 0:
        ma_s = pxf.tail(trend_stock).mean()
        pivot["위추세"] = (last_px.reindex(pivot.index)
                        >= ma_s.reindex(pivot.index))
    else:
        pivot["위추세"] = True

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

    cols = ["ROE", "GPA", "CFO", "PER", "PBR", "PCR", "PSR", "DY", "12M",
            "K_ratio", "VOL", "위추세"]
    merged = base.merge(pivot[cols].reset_index().rename(columns={sym_col: "symbol"}),
                        on="symbol", how="inner")
    merged = merged.drop_duplicates("symbol")
    if len(merged) < 30:
        return None

    scored = fc.build_scores(merged, symbol="symbol", sector="sector",
                             weights=weights or config.QVM_WEIGHTS,
                             n_portfolio=len(merged), neutral=neutral)
    # 추세 필터는 여기서 종목을 빼지 않습니다.
    #
    # 전에는 scored에서 아예 제거했는데, 유지 판정 명단(hold_set)도 같은
    # scored에서 만들기 때문에 보유 종목이 200일선을 한 번 밑돌면 순위와
    # 무관하게 강제 매도됐습니다. 순위 버퍼가 무력화되고 회전율이 연 152%
    # → 593%로 뛰었습니다. 그래서 '위추세' 컬럼만 남기고, 새로 살 때만
    # 거릅니다 (버퍼와 같은 사고방식: 안 사는 것과 파는 것은 다른 규칙).
    return scored.dropna(subset=["qvm"])


# ── 본체 ─────────────────────────────────────────────────────
def run(market: str = "kr", top_n: int = 30, rebalance: str = "ME",
        cost_bps: float = 25.0, lag_days: int = 90,
        max_sector_pct: float = 1.0, min_turnover: float = 0.0,
        tranches: int = 1, trend_ma: int = 0, risk_off: float = 0.0,
        cash_rate: float = 0.02, weighting: str = "equal",
        max_weight: float = 0.0, rebal_band: float = 0.0,
        rank_buffer: float = 0.0, sector_neutral=None,
        use_lowvol: bool = False, trend_stock: int = 0,
        max_sector_weight: float = 0.0, qvm_weights=None,
        split=None) -> dict:
    """tranches=K 면 자금을 K등분해 매 회차 1/K만 점검합니다(분할 리밸런싱).

    주기를 월말에서 주간으로 줄이면 한 번에 포트폴리오 전체가 바뀌어
    거래가 커지고 회전율도 그만큼 뜁니다. 분할 리밸런싱은 주기는 주간으로
    두되 매주 한 등분만 손보기 때문에,
      · 한 번의 거래가 작고 (100종목 4등분이면 25종목 중 이탈분만)
      · 각 종목의 점검 간격은 여전히 4주라 회전율이 안 늘고
      · 진입 시점이 4주에 흩어져 특정 하루의 운을 덜 탑니다.
    K=1이면 예전 동작과 완전히 같습니다.

    trend_ma > 0 이면 리스크 오버레이를 켭니다. 시장지수가 trend_ma일
    이동평균 아래면 주식 비중을 risk_off(기본 0 = 전량 현금)로 줄이고,
    남은 돈은 cash_rate(연) 이자를 받습니다. 낙폭을 줄이는 게 목적입니다.

    파라미터는 일부러 표준값(200일)만 쓰고 튜닝하지 않습니다. 여러 값을
    돌려 제일 좋은 걸 고르면 그 숫자는 과거에만 맞습니다.

    weighting은 종목별 비중 방식입니다(equal/mcap/score/invvol). max_weight는
    한 종목 상한이고 0이면 방식별 기본값을 씁니다.

    ※ 회전율 정의가 바뀌었습니다. 예전에는 '교체된 종목 수 ÷ 보유 종목 수'
    였는데, 지금은 비중 변화량(Σ|Δw|÷2)입니다. 들고 있는 종목을 목표 비중으로
    되돌리는 거래가 예전 정의에서는 공짜였습니다. 그래서 같은 설정이라도
    예전 실행보다 회전율과 비용이 높게 나옵니다. 예전 숫자와는 비교하지 말고
    equal 기준선을 다시 돌려서 비교하세요.
    """
    spec = SPECS[market]
    weighting = str(weighting or "equal").lower()
    if weighting not in WEIGHTINGS:
        raise ValueError(f"weighting은 {WEIGHTINGS} 중 하나여야 합니다 "
                         f"(받은 값 {weighting!r})")
    cap = float(max_weight) if max_weight and max_weight > 0 else \
        _DEFAULT_CAP.get(weighting, 0.0)
    band = max(0.0, float(rebal_band))
    buffer = max(0.0, float(rank_buffer))
    neutral = fc.neutral_spec(sector_neutral
                              if sector_neutral is not None
                              else config.SECTOR_NEUTRAL)
    qvm_weights = (list(qvm_weights) if qvm_weights
                   else (config.QVML_WEIGHTS if use_lowvol
                         else config.QVM_WEIGHTS))
    # 교차 분할 — 같은 순위 명단을 번갈아 나눠 갖습니다 (종목 0% 겹침).
    sp = dict(split or {})
    split_mod = max(1, int(sp.get("mod", 1) or 1))
    split_rem = int(sp.get("rem", 0) or 0) % split_mod
    # shared: 두 계좌가 함께 보는 종목 비중 (1.0이면 분할 없음)
    split_shared = sp.get("shared")
    split_shared = (None if split_shared is None
                    else min(1.0, max(0.0, float(split_shared))))
    if split_shared is not None and split_shared >= 1.0:
        split_mod = 1
    sec_w_cap = max(0.0, float(max_sector_weight))
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

    shares = _shares(ticker, spec, price_pivot)

    # 전역 섹터 지도 — 보유 종목의 섹터는 점수표에 그 종목이 있든 없든
    # 항상 알 수 있어야 합니다 (섹터 비중 상한이 '기타' 뒤로 숨지 않도록).
    sector_map_all: dict = {}
    if sector is not None and spec.sector_key in sector.columns:
        sector_map_all = dict(zip(sector[spec.sector_key].astype(str),
                                  sector[spec.sector_col].astype(str)))
    elif spec.sector_col in ticker.columns:
        sector_map_all = dict(zip(ticker[spec.t_sym].astype(str),
                                  ticker[spec.sector_col].astype(str)))

    # 리스크 오버레이용 시장지수 — 전 종목 동일가중 일간 수익률의 누적.
    # 시총가중 대신 동일가중을 쓰는 이유: 소수 대형주가 아니라 '시장 전체가
    # 내려가고 있는가'를 봐야 위험 신호로 쓸 수 있기 때문입니다.
    mkt = ma = None
    if trend_ma and trend_ma > 0:
        dret = price_pivot.ffill().pct_change(fill_method=None)
        mkt = (1 + dret.mean(axis=1).fillna(0)).cumprod()
        ma = mkt.rolling(trend_ma, min_periods=trend_ma // 2).mean()

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

    rows, holdings_log, sel_log, ic_log = [], [], [], []
    hold_sizes: list[int] = []
    ttm_cache = {}

    # 분할 리밸런싱 상태 — 등분별 보유 종목
    n_tr = max(1, int(tranches))
    sizes = [top_n // n_tr + (1 if j < top_n % n_tr else 0) for j in range(n_tr)]
    sleeves: list[list[str]] = [[] for _ in range(n_tr)]
    started = False
    prev_expo = 0.0          # 직전 회차의 주식 비중 (거래량 계산용)
    w_state = None           # 직전 회차 말 기준 실제 비중 (주가 변동 반영 후)

    for i, t in enumerate(dates[:-1]):
        t_next = dates[i + 1]
        scored = _score_at(t, price_pivot, fs, ticker, sector, shares, spec,
                           lag_days, ttm_cache, neutral=neutral,
                           weights=qvm_weights, trend_stock=trend_stock)
        if scored is None or scored.empty:
            continue

        eligible = None
        if turnover_pivot is not None:
            win = turnover_pivot.loc[:t].tail(config.LIQUIDITY_WINDOW)
            avg = win.mean()
            eligible = set(avg[avg >= min_turnover].index)

        # 교차 분할 — 종목코드 해시로 고정 배정합니다. 순위로 나누면 순위가
        # 한 칸 움직일 때 소속이 뒤바뀌고, 4주간 들고 있는 동안 두 계좌가 같은
        # 종목을 동시에 보유합니다 (순위 방식 실측: 회차당 최대 17종목 겹침).
        if split_mod > 1:
            if split_shared is None:
                mine = scored["symbol"].astype(str).map(
                    lambda c: fc.split_bucket(c, split_mod) == split_rem)
            else:
                mine = scored["symbol"].astype(str).map(
                    lambda c: fc.split_keep(c, split_shared, split_rem,
                                            split_mod))
            scored = scored[mine]

        # 매수 후보 — 추세 필터는 여기에만 걸립니다 (유지 판정은 아래 hold_set)
        buy_pool = scored
        trend_blocked = 0
        if trend_stock and trend_stock > 0 and "위추세" in scored.columns:
            up = scored["위추세"].fillna(True).astype(bool)
            trend_blocked = int((~up).sum())
            if int(up.sum()) >= max(30, top_n):
                buy_pool = scored[up]

        picks, sel = fc.select_portfolio(
            buy_pool, symbol="symbol", sector="sector", n=top_n,
            max_sector_pct=max_sector_pct, eligible=eligible)
        sel.update({"추세탈락": trend_blocked,
                    # 같은 설정인데 성과가 달라졌을 때 가장 먼저 봐야 하는 값.
                    # 점수가 계산된 종목 수가 바뀌면 유니버스가 바뀐 것이고,
                    # 그러면 성과가 달라지는 게 당연합니다.
                    "점수산출종목": int(len(scored))})
        sel_log.append({"리밸런싱일": t, **sel})

        # 유지 판정용 넓은 명단 (순위 버퍼). qvm이 작을수록 좋은 종목입니다.
        hold_set: set = set()
        if buffer > 0:
            # 유지 판정은 추세와 무관하게 순위만 봅니다 — scored 전체 사용
            pool_sc = scored
            if eligible:
                pool_sc = scored[scored["symbol"].astype(str).isin(eligible)]
            hold_rank = int(round(top_n * (1 + buffer)))
            hold_set = set(pool_sc.nsmallest(hold_rank, "qvm")["symbol"].astype(str))
            hold_sizes.append(len(hold_set))

        target = [str(c) for c in picks["symbol"] if c in price_pivot.columns]
        if len(target) < 5:
            continue

        slot = None
        prev_sleeves = [list(s) for s in sleeves]
        if not started:
            # 첫 회차는 전액을 한 번에 넣습니다. 순위를 등분에 번갈아 나눠
            # 어느 한 등분만 상위권을 독차지하지 않게 합니다.
            for j, c in enumerate(target[:top_n]):
                sleeves[j % n_tr].append(c)
            started = True
        else:
            slot = i % n_tr
            # 순위 버퍼 — 살 때는 상위 top_n, 팔 때는 top_n×(1+buffer) 밖으로
            # 밀려나야 팝니다. 측정해 보니 회전율의 89%가 종목 교체였고, 그
            # 교체는 100위 경계를 들락날락하는 종목들입니다(99위 → 103위에
            # 팔고 다음 달 98위에 되사기). 버퍼를 두면 그 왕복이 사라집니다.
            # 주의: target은 select_portfolio가 돌려준 top_n종목뿐입니다. 그래서
            # 유지 판정용 명단은 점수표에서 따로 뽑습니다(섹터 상한 없이 순위만).
            # 섹터 상한은 '새로 담을 때' 지키는 규칙이고, 이미 들고 있는 종목을
            # 억지로 팔아야 할 이유는 아닙니다.
            tset = hold_set if buffer > 0 else set(target)
            others = {c for j, s in enumerate(sleeves) if j != slot for c in s}
            keep = [c for c in sleeves[slot] if c in tset]     # 버퍼 안이면 유지
            need = sizes[slot] - len(keep)
            if need > 0:
                blocked = set(keep) | others
                keep += [c for c in target if c not in blocked][:need]
            sleeves[slot] = keep[:sizes[slot]]

        codes = list(dict.fromkeys(c for s in sleeves for c in s))
        if len(codes) < 5:
            continue

        window = price_pivot.loc[t:t_next, codes].ffill()
        if len(window) < 2:
            continue
        rets = (window.iloc[-1] / window.iloc[0] - 1).dropna()
        if rets.empty:
            continue

        # ── 비중 결정 ────────────────────────────────────────
        # 점수가중의 순위는 '전체 점수표' 기준으로 매깁니다. 상위 100종목
        # 안에서만 매기면, 버퍼 구간(101~150위) 종목이 꼴찌 비중을 받고
        # 100위 안에 들어오는 순간 비중이 급등합니다. 종목 교체를 줄이려고
        # 버퍼를 넣었는데 그 왕복이 비중 쪽으로 옮겨갈 뿐입니다.
        # 섹터 지도는 전역 지도를 먼저 쓰고 그 시점 점수표로 보완합니다.
        # scored만 쓰면 필터로 빠진 보유 종목이 '기타'로 뭉쳐서, 상한이
        # 정작 쏠린 쪽을 못 봅니다 (실제로 최악 49.6%가 그 '기타'였습니다).
        sec_map = None
        if sec_w_cap > 0:
            sec_map = dict(sector_map_all)
            sec_map.update(dict(zip(scored["symbol"].astype(str),
                                    scored["sector"])))
        rank_order = (scored.dropna(subset=["qvm"])
                      .sort_values("qvm")["symbol"].astype(str).tolist())
        tw = _target_weights(codes, weighting, shares=shares,
                             price_pivot=price_pivot, t=t, order=rank_order,
                             cap=cap, vol_window=config.VOL_WINDOW)

        if w_state is None or slot is None:
            w_new = tw                      # 첫 회차 — 전량을 목표 비중대로
        else:
            # 분할 리밸런싱의 핵심: 이번 주에 손보는 등분만 목표 비중으로
            # 다시 맞추고, 나머지 등분은 주가가 움직인 그대로 둡니다. 매주
            # 100종목 전부를 목표 비중으로 되돌리면 그게 바로 '전체 리밸런싱'
            # 이고, 분할로 거래를 줄이려던 목적이 사라집니다.
            sl = [c for c in sleeves[slot] if c in rets.index]
            keep_out = [c for c in codes if c not in set(sleeves[slot])]
            # 이번 등분에 묶여 있던 돈 — 팔 종목까지 포함해 그 몫만 재배분
            pool = float(w_state.reindex(prev_sleeves[slot]).fillna(0.0).sum())
            w_new = pd.Series(0.0, index=pd.Index(codes), dtype="float64")
            if keep_out:
                w_new.loc[keep_out] = w_state.reindex(keep_out).fillna(0.0).values
            if sl and pool > 0:
                sub = tw.reindex(sl).fillna(0.0)
                ssum = float(sub.sum())
                sub = (sub / ssum) if ssum > 0 else pd.Series(
                    1.0 / len(sl), index=pd.Index(sl), dtype="float64")
                want = pool * sub                      # 목표 비중
                if band > 0:
                    # 무매매 밴드 — 목표에서 band 이내로 벗어난 종목은
                    # 그대로 둡니다. 실전에서 1%가 1.1%가 된 걸 매주 되돌리면
                    # 거래세만 나갑니다. 이 밴드가 없던 탓에 회전율이 연 249%
                    # 까지 뛰었고, 그 비용이 초과수익의 90%를 먹었습니다.
                    cur = w_state.reindex(pd.Index(sl)).fillna(0.0)
                    hold = (cur > 0) & ((cur - want).abs() <= band * want)
                    locked = float(cur[hold].sum())
                    rest = pool - locked
                    if rest < 0:
                        # 그대로 둔 종목이 너무 커져 등분 몫을 넘은 경우 —
                        # 비례로 눌러 등분 합을 지킵니다
                        cur.loc[hold] = cur[hold] * (pool / max(locked, 1e-12))
                        locked, rest = pool, 0.0
                    out = [c for c in sl if not bool(hold.get(c, False))]
                    new_w = cur.copy()
                    if out:
                        ow = want.reindex(out).fillna(0.0)
                        osum = float(ow.sum())
                        new_w.loc[out] = (rest * (ow / osum)).values if osum > 0 \
                            else rest / len(out)
                    want = new_w
                w_new.loc[sl] = want.reindex(pd.Index(sl)).fillna(0.0).values
            tot = float(w_new.sum())
            w_new = (w_new / tot) if tot > 0 else tw

        # ── 보유 기준 섹터 비중 상한 ────────────────────────
        # select_portfolio의 섹터 상한은 '살 때' 종목 수만 제한합니다.
        # 4등분으로 나눠 사고 순위 버퍼로 계속 들고 있으면 보유가 누적돼
        # 상한을 넘습니다 — 캡 40%를 걸고도 한 섹터가 54%까지 갔습니다.
        # 이건 비중 자체를 누르고, 깎은 몫을 다른 섹터에 비례 배분합니다.
        sec_trim = 0.0
        if sec_w_cap > 0 and sec_map:
            secs = pd.Series({c: sec_map.get(c, "기타") for c in w_new.index})
            for _ in range(20):
                tot_by = w_new.groupby(secs).sum()
                over = tot_by[tot_by > sec_w_cap + 1e-12]
                if over.empty:
                    break
                for sname, sw in over.items():
                    mem = secs[secs == sname].index
                    scale = sec_w_cap / float(sw)
                    sec_trim += float(w_new.loc[mem].sum()) * (1 - scale)
                    w_new.loc[mem] = w_new.loc[mem] * scale
                free = secs[~secs.isin(over.index)].index
                room = 1.0 - float(w_new.sum())
                if len(free) and room > 0:
                    base = float(w_new.loc[free].sum())
                    if base > 0:
                        w_new.loc[free] += room * (w_new.loc[free] / base)
                    else:
                        w_new.loc[free] += room / len(free)
                else:
                    break
            tot = float(w_new.sum())
            if tot > 0:
                w_new = w_new / tot

        # 상한을 크게 넘은 종목은 차례를 기다리지 않고 깎습니다.
        # 깎는 거래는 아래 회전율에 그대로 잡혀 비용을 뭅니다.
        breach = 0
        if 0 < cap < 1:
            hard = cap * CAP_BREACH_MULT
            if float(w_new.max()) > hard:
                breach = int((w_new > hard).sum())
                w_new = _cap_weights(w_new, cap)

        # 회전율 = 비중 변화량의 절반(= 한쪽 방향 거래 비중).
        if w_state is None:
            turnover = 1.0
        else:
            u = w_new.index.union(w_state.index)
            turnover = float((w_new.reindex(u).fillna(0.0)
                              - w_state.reindex(u).fillna(0.0)).abs().sum()) / 2

        wv = w_new.reindex(rets.index).fillna(0.0)
        wsum = float(wv.sum())
        if wsum <= 0:
            continue
        wv = wv / wsum
        port_ret = float((rets * wv).sum())

        # 다음 회차를 위한 비중 갱신 — 주가가 움직인 만큼 저절로 바뀝니다
        grown = wv * (1.0 + rets)
        gsum = float(grown.sum())
        w_state = grown / gsum if gsum > 0 else wv
        top10 = float(wv.sort_values(ascending=False).head(10).sum())
        wmax = float(wv.max())
        max_sec_w = 0.0
        if sec_map:
            max_sec_w = float(wv.groupby(
                pd.Series({c: sec_map.get(c, "기타") for c in wv.index})
            ).sum().max())

        # 벤치마크 — 같은 기간 전 종목 동일가중
        bwin = price_pivot.loc[t:t_next].ffill()
        brets = (bwin.iloc[-1] / bwin.iloc[0] - 1).dropna()
        bench_ret = float(brets.mean()) if len(brets) else 0.0

        # 두 번째 벤치마크 — '살 수 있었던 종목'만 동일가중.
        #
        # 전 종목 벤치마크에는 거래대금 몇천만원짜리도 들어갑니다. 우리는 그런
        # 종목을 애초에 못 사는데, 그 차이가 전부 초과수익으로 잡힙니다. 즉
        # 지금 재고 있는 것은 '모델의 종목 선택 실력'이 아니라 거기에
        # '소형·비유동주를 피한 효과'가 섞인 값입니다. 추적오차도 그만큼 부풀죠.
        # 유동성을 통과한 종목만으로 다시 재면 모델이 고른 실력만 남습니다.
        pool = ([c for c in eligible if c in brets.index] if eligible
                else list(brets.index))
        bench_liq = float(brets.reindex(pool).dropna().mean()) if pool else bench_ret

        # 세 번째 벤치마크 — 시총 상위 200종목 시총가중 (코스피200 근사).
        #
        # 이게 진짜 비교 대상입니다. 앞의 두 벤치마크는 '전 종목을 똑같이 샀다면'
        # 이라는 가상의 포트폴리오인데, 실제 대안은 지수 ETF를 사는 것입니다.
        # 같은 날짜·같은 방식으로 재야 낙폭까지 사과끼리 비교됩니다 (월말로 잰
        # 낙폭과 주간으로 잰 낙폭은 비교할 수 없습니다).
        bench_idx = bench_ret
        if len(shares):
            mc = (price_pivot.loc[:t].ffill().iloc[-1]
                  * shares.reindex(price_pivot.columns))
            mc = mc.replace([np.inf, -np.inf], np.nan).dropna()
            big = mc.nlargest(INDEX_N).index
            iw = mc.reindex(big)
            ir = brets.reindex(big)
            ok_i = ir.notna() & iw.notna() & (iw > 0)
            if int(ok_i.sum()) >= 20:
                w_i = iw[ok_i] / float(iw[ok_i].sum())
                bench_idx = float((ir[ok_i] * w_i).sum())

        # 점수가 수익률 순위를 맞추는가 (IC) — 상위 N종목으로 압축하기 전에,
        # 그 시점 전 종목의 순위 정보를 그대로 씁니다. 관측이 수백 배 많아
        # 포트폴리오 수익률보다 훨씬 예민하게 신호 유무를 가려냅니다.
        ic = q_spread = np.nan
        q_means = {}
        sc_pool = scored[scored["symbol"].astype(str).isin(pool)] if pool else scored
        sc_pool = sc_pool.dropna(subset=["qvm"])
        fwd = sc_pool["symbol"].astype(str).map(brets)
        ok = fwd.notna()
        if int(ok.sum()) >= 50:
            qv, fw = sc_pool.loc[ok, "qvm"], fwd[ok]
            ic = float(spearmanr(-qv, fw).statistic)
            try:
                bucket = pd.qcut(qv.rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
                q_means = {int(k): float(v) for k, v in fw.groupby(bucket, observed=True).mean().items()}
                if 1 in q_means and 5 in q_means:
                    q_spread = q_means[1] - q_means[5]
            except ValueError:
                pass
        ic_log.append({"리밸런싱일": t, "검사종목": int(ok.sum()), "IC": ic,
                       "분위스프레드": q_spread,
                       **{f"{k}분위": v for k, v in sorted(q_means.items())}})

        # ── 리스크 오버레이 ──────────────────────────────────
        # 그날까지의 시장지수만 봅니다(미래 미사용). 이동평균 아래면 비중 축소.
        expo = 1.0
        if ma is not None:
            cur = mkt.loc[:t]
            cur_ma = ma.loc[:t].dropna()
            if len(cur_ma):
                expo = 1.0 if float(cur.iloc[-1]) >= float(cur_ma.iloc[-1]) else risk_off

        # 거래량: 비중을 바꾼 만큼은 통째로 사고팔고, 계속 들고 있는 부분에서는
        # 종목 교체분만 거래합니다.
        traded = abs(expo - prev_expo) + min(expo, prev_expo) * turnover
        cost = traded * (cost_bps / 10000) * 2        # 팔고 사는 왕복
        prev_expo = expo

        days = max((t_next - t).days, 1)
        cash_ret = (1 + cash_rate) ** (days / 365.25) - 1
        net_ret = expo * port_ret + (1 - expo) * cash_ret - cost

        rows.append({"리밸런싱일": t, "다음리밸런싱": t_next,
                     "종목수": len(codes), "수익률": port_ret,
                     "비용차감수익률": net_ret, "주식비중": expo,
                     "벤치마크": bench_ret, "벤치마크_유동성": bench_liq,
                     "벤치마크_지수": bench_idx,
                     "턴오버": turnover, "최대종목비중": wmax,
                     "상위10비중": top10, "상한초과정리": breach,
                     "최대섹터비중": max_sec_w, "섹터상한깎음": sec_trim})

        info = scored.drop_duplicates("symbol").set_index("symbol")
        info.index = info.index.astype(str)
        info = info.reindex(codes)
        holdings_log.append(pd.DataFrame({
            "리밸런싱일": t, "종목코드": codes,
            "종목명": info["name"].values, "섹터": info["sector"].values,
            "qvm": info["qvm"].round(4).values}))

    if not rows:
        raise RuntimeError("유효한 리밸런싱이 한 번도 없었습니다.")

    perf = pd.DataFrame(rows)
    perf["누적"] = (1 + perf["비용차감수익률"]).cumprod()
    perf["누적_벤치마크"] = (1 + perf["벤치마크"]).cumprod()
    perf["누적_벤치마크_유동성"] = (1 + perf["벤치마크_유동성"]).cumprod()

    summary = _metrics(perf, spec, top_n, cost_bps, lag_days)
    summary["통계"] = _stats_block(perf, pd.DataFrame(ic_log))
    summary["제약"] = {
        "비중방식": {"equal": "동일가중 (전 종목 같은 금액)",
                 "mcap": "시총가중 (큰 회사를 크게)",
                 "score": "점수가중 (qvm 순위에 선형)",
                 "invvol": f"역변동성 ({config.VOL_WINDOW}일 변동성의 역수)",
                 }[weighting],
        "교차분할": ("없음" if split_mod <= 1
                 else (f"종목코드 해시 {split_mod}등분 중 {split_rem + 1}번째 "
                       f"— 다른 계좌와 겹침 0" if split_shared is None
                       else f"공용 {split_shared:.0%} + 전용 "
                            f"{1 - split_shared:.0%}({split_mod}등분 중 "
                            f"{split_rem + 1}번째)")),
        "팩터가중치": "/".join(f"{w:.2f}" for w in qvm_weights),
        "저변동성팩터": ("켬 (QVML — 네 팩터 1/4씩)" if use_lowvol
                     else "끔 (QVM — 변동성 정보 없음)"),
        "종목추세필터": ("없음" if not trend_stock or trend_stock <= 0
                   else f"종목이 자기 {trend_stock}일 이동평균 아래면 "
                        f"'새로 사지 않음' (보유분은 순위 버퍼대로 유지)"),
        "보유섹터비중상한": ("없음 — 살 때 종목 수만 제한(누적되면 넘침)"
                     if sec_w_cap <= 0 else f"한 섹터 보유비중 최대 {sec_w_cap:.0%}"),
        "섹터중립": ("모든 팩터를 섹터 안에서만 비교"
                 if set(neutral) == set(fc.SECTOR_NEUTRAL_ALL)
                 else ("섹터 중립 없음 — 전 종목 비교" if not neutral
                       else f"섹터 안에서만 비교: {', '.join(neutral)}"
                            f" / 전 종목 비교: "
                            f"{', '.join(x for x in fc.SECTOR_NEUTRAL_ALL if x not in neutral)}")),
        "유지명단 크기": (f"평균 {int(np.mean(hold_sizes))}종목"
                      if hold_sizes else "버퍼 없음"),
        "순위버퍼": ("없음 — 상위 종목 수 밖으로 밀려나면 바로 매도"
                 if buffer <= 0
                 else f"상위 {top_n}위 안에서 사고, "
                      f"{int(round(top_n * (1 + buffer)))}위 밖으로 밀려나면 매도"),
        "무매매밴드": ("없음 — 손보는 등분을 매번 목표 비중으로 되맞춤"
                   if band <= 0
                   else f"목표의 ±{band:.0%} 안이면 그대로 둠"),
        "종목상한": ("없음" if cap <= 0 or cap >= 1
                  else (f"한 종목 최대 {cap:.1%}" if top_n * cap >= 1
                        # 상한 × 종목수 < 1 이면 상한을 지키면서 100%를 채울 수
                        # 없습니다. 조용히 무시되면 '상한을 걸었다'고 착각하게
                        # 되므로 요약에 그대로 적습니다.
                        else f"설정 {cap:.1%}인데 {top_n}종목으로는 지킬 수 없어 "
                             f"무시됨 (상한 × 종목수 = {top_n * cap:.2f} < 1)")),
        "섹터상한": ("없음" if max_sector_pct >= 1
                  else f"한 섹터 최대 {max_sector_pct:.0%}"),
        "유동성기준": ("없음" if min_turnover <= 0
                   else f"{config.LIQUIDITY_WINDOW}일 평균 거래대금 {min_turnover:,.0f} 이상"),
        "분할리밸런싱": ("없음 (매 회차 전체 교체)" if n_tr <= 1
                    else f"{n_tr}등분 — 매 회차 {sizes[0]}종목만 점검"),
        "리스크오버레이": ("없음 (항상 100% 투자)" if not trend_ma or trend_ma <= 0
                    else f"시장지수 {trend_ma}일 이동평균 아래면 주식비중 {risk_off:.0%}"
                         f" · 현금 이자 연 {cash_rate:.1%}"),
    }
    if ("벤치마크_지수" in perf.columns
            and float((perf["벤치마크_지수"] - perf["벤치마크"]).abs().max()) < 1e-12):
        summary["한계"].insert(0, "지수 벤치마크를 만들지 못해 '전종목 동일가중'과 "
                                 "같은 값입니다 (주식수를 못 구함). 지수 대비 "
                                 "숫자를 쓰지 마세요")
    if "최대섹터비중" in perf.columns and float(perf["최대섹터비중"].max()) > 0:
        summary["제약"]["실제 최대섹터비중 평균"] = f"{perf['최대섹터비중'].mean():.1%}"
        summary["제약"]["실제 최대섹터비중 최악"] = f"{perf['최대섹터비중'].max():.1%}"
    if "최대종목비중" in perf.columns:
        summary["제약"]["실제 최대종목비중 평균"] = f"{perf['최대종목비중'].mean():.2%}"
        summary["제약"]["실제 최대종목비중 최악"] = f"{perf['최대종목비중'].max():.2%}"
        summary["제약"]["실제 상위10종목 비중 평균"] = f"{perf['상위10비중'].mean():.1%}"
        if cap > 0:
            summary["제약"]["상한 초과로 중간 정리한 회차"] = (
                f"{int((perf['상한초과정리'] > 0).sum())}/{len(perf)}")
    if trend_ma and trend_ma > 0:
        summary["제약"]["주식비중 평균"] = f"{perf['주식비중'].mean():.0%}"
        summary["제약"]["현금 보유 회차"] = f"{int((perf['주식비중'] < 1).sum())}/{len(perf)}"
    if sel_log:
        sl = pd.DataFrame(sel_log)
        for k in ("점수산출종목", "유동성탈락", "섹터상한탈락", "추세탈락"):
            if k in sl.columns and sl[k].notna().any():
                summary["제약"][f"평균 {k}"] = int(sl[k].mean())
        if "점수산출종목" in sl.columns:
            summary["제약"]["점수산출종목 범위"] = (
                f"{int(sl['점수산출종목'].min())} ~ {int(sl['점수산출종목'].max())}")
    # 연도별 성과 — 전체 누적 한 줄로는 '언제 이기고 언제 지는지'가 안 보입니다.
    yearly = pd.DataFrame()
    if len(perf):
        yp = perf.copy()
        yp["연"] = pd.to_datetime(yp["리밸런싱일"]).dt.year
        cols = {"포트폴리오%": "비용차감수익률", "전종목%": "벤치마크"}
        if "벤치마크_지수" in yp.columns:
            cols["지수%"] = "벤치마크_지수"
        yearly = yp.groupby("연").apply(
            lambda x: pd.Series({k: ((1 + x[c]).prod() - 1) * 100
                                 for k, c in cols.items()}),
            include_groups=False).round(2)
        if "지수%" in yearly.columns:
            yearly["지수대비%p"] = (yearly["포트폴리오%"] - yearly["지수%"]).round(2)
        yearly["전종목대비%p"] = (yearly["포트폴리오%"] - yearly["전종목%"]).round(2)
        yearly = yearly.reset_index()

    holdings = pd.concat(holdings_log, ignore_index=True) if holdings_log else pd.DataFrame()

    return {"market": market, "summary": summary, "perf": perf,
            "holdings": holdings, "ic": pd.DataFrame(ic_log), "yearly": yearly}


def _tstat(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    if len(x) < 3 or float(x.std(ddof=1)) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def _stats_block(perf: pd.DataFrame, ic: pd.DataFrame) -> dict:
    """t값과 그 분해 — 무엇을 고쳐야 t가 오르는지 보이게.

    t = IR x √기간 이고 IR = 초과수익 / 추적오차 입니다. t가 낮을 때
    '초과수익이 작아서'인지 '추적오차가 커서'인지에 따라 할 일이 완전히
    다릅니다. 둘을 따로 보여줍니다.

    IC(정보계수)는 상위 N종목으로 압축하기 전, 전 종목 순위로 잰 신호 강도라
    관측이 수백 배 많습니다. IC는 뚜렷한데 포트폴리오 t가 낮다면 문제는
    신호가 아니라 포트폴리오 구성(종목 수·가중·제약)에 있습니다.
    """
    per_year = len(perf) / max((perf["다음리밸런싱"].iloc[-1]
                                - perf["리밸런싱일"].iloc[0]).days / 365.25, 1e-9)
    out = {}
    for label, col in (("전종목 대비", "벤치마크"),
                       ("유동성통과 대비", "벤치마크_유동성"),
                       ("지수 대비", "벤치마크_지수")):
        if col not in perf.columns:
            continue
        ex = perf["비용차감수익률"] - perf[col]
        te = float(ex.std(ddof=1)) * np.sqrt(per_year)
        exr = float(ex.mean()) * per_year
        out[f"{label} 연초과"] = round(exr * 100, 2)
        out[f"{label} 추적오차"] = round(te * 100, 2)
        out[f"{label} IR"] = round(exr / te, 3) if te else None
        out[f"{label} t"] = round(_tstat(ex), 2)
    if len(ic) and ic["IC"].notna().any():
        out["IC 평균"] = round(float(ic["IC"].mean()), 4)
        out["IC t"] = round(_tstat(ic["IC"]), 2)
        out["IC 양수비율"] = round(float((ic["IC"] > 0).mean()) * 100, 1)
        out["분위스프레드 t"] = round(_tstat(ic["분위스프레드"]), 2)
        qs = [c for c in ic.columns if c.endswith("분위")]
        if qs:
            out["분위별 평균수익%"] = {c: round(float(ic[c].mean()) * 100, 2) for c in qs}
    return out

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
    idx = (stats((1 + perf["벤치마크_지수"]).cumprod(), perf["벤치마크_지수"])
           if "벤치마크_지수" in perf.columns else None)

    return {
        "시장": spec.label,
        "기간": f"{perf['리밸런싱일'].iloc[0]:%Y-%m-%d} ~ {perf['다음리밸런싱'].iloc[-1]:%Y-%m-%d}",
        "리밸런싱횟수": int(len(perf)),
        "보유종목수": top_n,
        "평균턴오버": round(float(perf["턴오버"].mean()) * 100, 1),
        # 주기가 다르면 회차당 턴오버는 비교가 안 됩니다(주간 5%와 월간 20%는
        # 같은 회전율). 연 단위로 환산해 나란히 놓을 수 있게 합니다.
        "연환산턴오버": round(float(perf["턴오버"].mean()) * (len(perf) / years) * 100, 0),
        "거래비용가정": f"편도 {cost_bps:.0f}bp",
        "공시지연가정": f"{lag_days}일",
        "포트폴리오": port,
        "벤치마크(전종목 동일가중)": bench,
        **({f"벤치마크(시총상위{INDEX_N} 시총가중 = 지수)": idx} if idx else {}),
        "초과수익률": round(port["누적수익률"] - bench["누적수익률"], 2),
        **({"지수대비 초과수익률":
            round(port["누적수익률"] - idx["누적수익률"], 2),
            "지수대비 낙폭차":
            round(port["최대낙폭"] - idx["최대낙폭"], 2)} if idx else {}),
        "한계": [
            "생존편향: 상장폐지 종목이 데이터에 없어 성과가 과대평가됩니다 (가장 큰 한계)",
            "과거 시가총액은 '그날 종가 × 현재 주식수' 근사 — 증자·감자·분할 미반영. "
            "시총가중을 쓰면 이 근사 오차가 성과에 직접 들어갑니다",
            "섹터와 주당배당금은 현재 값을 과거에도 적용",
            "최대낙폭은 리밸런싱 시점에서만 재므로 주기가 다르면 비교할 수 없습니다 "
            "(월말로 재면 월중 낙폭을 건너뜁니다). 주기가 다른 설정끼리는 "
            "'벤치마크 대비 낙폭'으로 비교하세요",
            "회전율 정의가 '교체 종목 수'에서 '비중 변화량(Σ|Δw|÷2)'으로 바뀌었습니다. "
            "들고 있는 종목을 목표 비중으로 되돌리는 거래가 예전에는 공짜였습니다. "
            "같은 설정이라도 예전 실행보다 비용이 높게 나오니 예전 숫자와 비교하지 마세요",
            "과거 성과가 미래 수익을 보장하지 않습니다",
        ],
    }
