# -*- coding: utf-8 -*-
"""실제 운용 성과 — 원장·보유기록·주가로 수익률과 규칙 준수를 계산합니다.

백테스트는 '이 규칙이 과거에 통했나'를 말합니다. 이 모듈은 '지금 내 계좌가
그 규칙대로 가고 있나'를 말합니다. 둘은 다른 질문이고, 벌어지는 지점이
전략이 망가지는 지점입니다.

설계에서 중요한 세 가지
--------------------------------------------------------------------
1. **시간가중수익률(TWR)로 잽니다.** 분할 진입 중에는 매주 돈을 더 넣습니다.
   평가액의 증가분을 그대로 수익이라고 하면 '입금'이 '수익'으로 잡힙니다
   (배우자 계좌: 4주에 걸쳐 1,000만원을 넣는 동안 평가액은 210만 → 1,000만
   으로 늘지만 수익률은 0에 가깝습니다). 그래서 매일 평가액 변화에서 그날의
   순입출금을 빼고 곱해 나갑니다 — 입금 일정과 무관한 '모델의 실력'이 남습니다.

2. **수량은 보유기록 스냅샷의 계단함수입니다.** 매주 재동기화가 그 날의
   보유 수량을 남기므로, 두 스냅샷 사이에는 앞 스냅샷의 수량을 그대로 씁니다.
   주마다 점이 하나씩 늘어 저절로 촘촘해집니다. 수량 칸이 비어 있던 주는
   평가금액 ÷ 그날 종가로 역산합니다.

3. **벤치마크는 백테스트와 같은 정의**입니다 — 시총 상위 200종목 시총가중
   (지수 근사). 정의가 다르면 '백테스트 대비 괴리'가 벤치마크 차이인지
   전략 차이인지 구분할 수 없습니다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

INDEX_N = 200          # backtest.INDEX_N과 같아야 합니다 (아래 테스트가 확인)
BUY_WORDS = ("매수", "현금투입", "비중확대")
SELL_WORDS = ("매도", "비중축소")


# ── 준비 ────────────────────────────────────────────────────────────
def price_pivot(price: pd.DataFrame, codes=None, *,
                date_col: str = "날짜", code_col: str = "종목코드",
                close_col: str = "종가") -> pd.DataFrame:
    """날짜 × 종목코드 종가 표. codes를 주면 그 종목만.

    빈 입력도 빈 표로 돌려줍니다 — 화면에서 주가를 못 받았을 때 여기서
    터지면 앱 전체가 멈춥니다(탭 하나만 비는 게 맞습니다).
    """
    if price is None or len(price) == 0 or not {
            date_col, code_col, close_col} <= set(price.columns):
        return pd.DataFrame()
    df = price[[date_col, code_col, close_col]].copy()
    df[code_col] = df[code_col].astype(str).str.zfill(6)
    if codes is not None:
        want = {str(c).zfill(6) for c in codes}
        df = df[df[code_col].isin(want)]
    df[date_col] = pd.to_datetime(df[date_col])
    out = (df.pivot_table(index=date_col, columns=code_col,
                          values=close_col, aggfunc="last")
           .sort_index())
    return out


def shares_outstanding(ticker: pd.DataFrame, *, code_col: str = "종목코드",
                       mcap_col: str = "시가총액", close_col: str = "종가",
                       asof_col: str = "기준일") -> pd.Series:
    """시가총액 ÷ 종가 = 주식수(근사). 지수 벤치마크 가중에 씁니다.

    backtest._shares와 같은 근사입니다. 종목표의 마지막 기준일만 씁니다 —
    과거 주식수를 쓰면 증자·감자가 지수에 가짜 수익으로 들어옵니다.
    """
    if ticker.empty:
        return pd.Series(dtype="float64")
    t = ticker
    if asof_col in t.columns:
        t = t[t[asof_col] == t[asof_col].max()]
    code = t[code_col].astype(str).str.zfill(6)
    mcap = pd.to_numeric(t[mcap_col], errors="coerce")
    close = pd.to_numeric(t[close_col], errors="coerce")
    sh = (mcap / close).replace([np.inf, -np.inf], np.nan)
    sh.index = code
    sh = sh[sh > 0]
    return sh.groupby(level=0).last()


# ── 보유 수량의 시간선 ──────────────────────────────────────────────
def positions_timeline(holdings_log: pd.DataFrame, account: str,
                       px: pd.DataFrame) -> pd.DataFrame:
    """재동기화 스냅샷 → 날짜 × 종목코드 보유수량 표 (스냅샷 날짜만).

    수량이 비어 있으면 평가금액 ÷ 그날 종가로 역산합니다. 그날 종가가 없으면
    (휴장일 기준으로 적었거나 거래정지) 직전 종가를 씁니다.
    """
    if holdings_log.empty:
        return pd.DataFrame()
    h = holdings_log[holdings_log["계좌"].astype(str) == str(account)].copy()
    if h.empty:
        return pd.DataFrame()
    h["기준일"] = pd.to_datetime(h["기준일"])
    h["종목코드"] = h["종목코드"].astype(str).str.zfill(6)
    h["수량"] = pd.to_numeric(h.get("수량"), errors="coerce")
    h["평가금액"] = pd.to_numeric(h["평가금액"], errors="coerce")

    ff = px.ffill() if len(px) else px
    rows = []
    for asof, g in h.groupby("기준일"):
        close = None
        if len(ff):
            upto = ff.loc[:asof]
            if len(upto):
                close = upto.iloc[-1]
        for _, r in g.iterrows():
            qty = r["수량"]
            if not np.isfinite(qty) or qty <= 0:
                p = close.get(r["종목코드"]) if close is not None else None
                qty = (r["평가금액"] / p) if (p and p > 0
                                           and np.isfinite(r["평가금액"])) else np.nan
            rows.append({"기준일": asof, "종목코드": r["종목코드"], "수량": qty})
    out = (pd.DataFrame(rows)
           .pivot_table(index="기준일", columns="종목코드", values="수량",
                        aggfunc="last")
           .sort_index())
    return out


def daily_value(positions: pd.DataFrame, px: pd.DataFrame,
                end=None) -> pd.Series:
    """일별 보유 평가액. 스냅샷 사이는 앞 스냅샷의 수량을 유지합니다."""
    if positions.empty or px.empty:
        return pd.Series(dtype="float64")
    # 주가가 스냅샷 날짜보다 하루 이틀 뒤처지는 일이 흔합니다(목요일 종가로
    # 받고 금요일 저녁에 재동기화). 그때 start를 스냅샷 날짜로 두면 거래일이
    # 하나도 안 잡혀 곡선이 조용히 비어 버립니다 — 마지막 거래일로 당깁니다.
    start = positions.index.min()
    if len(px) and start > px.index.max():
        start = px.index.max()
    end = pd.Timestamp(end) if end is not None else px.index.max()
    days = px.loc[(px.index >= start) & (px.index <= end)].index
    if not len(days):
        return pd.Series(dtype="float64")
    # 스냅샷을 거래일에 맞춰 계단식으로 펼칩니다 (reindex+ffill).
    qty = positions.reindex(positions.index.union(days)).ffill().reindex(days)
    close = px.reindex(index=days).ffill()
    cols = [c for c in qty.columns if c in close.columns]
    if not cols:
        return pd.Series(dtype="float64")
    v = (qty[cols].fillna(0.0) * close[cols]).sum(axis=1, min_count=1)
    return v.dropna()


# ── 현금흐름과 시간가중수익률 ───────────────────────────────────────
def net_flows(trades: pd.DataFrame, account: str) -> pd.Series:
    """체결일별 순매수금액(+ 매수, - 매도). 체결된 건만 셉니다.

    체결가가 없는 행은 **일어나지 않은 거래**이거나 아직 체결내역을 안 넣은
    것입니다. 어느 쪽이든 현금흐름이 아니므로 제외합니다 — 계획가로 넣으면
    수익률에 없는 거래가 섞입니다.
    """
    if trades.empty:
        return pd.Series(dtype="float64")
    t = trades[trades["계좌"].astype(str) == str(account)].copy()
    px = pd.to_numeric(t.get("체결가"), errors="coerce")
    qty = pd.to_numeric(t.get("체결수량"), errors="coerce")
    ok = px.notna() & qty.notna() & (qty > 0)
    t, px, qty = t[ok], px[ok], qty[ok]
    if t.empty:
        return pd.Series(dtype="float64")
    kind = t["구분"].astype(str)
    sign = np.where(kind.str.contains("|".join(SELL_WORDS), na=False), -1.0, 1.0)
    when = pd.to_datetime(t["체결일"]).dt.normalize()
    flow = pd.Series(sign * px.to_numpy() * qty.to_numpy(), index=when.to_numpy())
    return flow.groupby(level=0).sum().sort_index()


def twr(value: pd.Series, flows: pd.Series | None = None) -> pd.DataFrame:
    """시간가중수익률. 입출금을 걷어내고 하루씩 곱해 나갑니다.

        r_t = (V_t - F_t) / V_(t-1) - 1

    F_t를 분모가 아니라 분자에서 빼는 이유: 입금이 장중 어느 시점에 들어왔는지
    모르므로 '그날 종가에 들어왔다'고 보는 쪽이 보수적입니다(수익률을 부풀리지
    않습니다).
    """
    v = value.dropna().sort_index()
    if len(v) < 1:
        return pd.DataFrame(columns=["평가액", "순입출금", "일수익률", "누적"])
    f = (flows if flows is not None else pd.Series(dtype="float64")).copy()
    f = f.reindex(v.index).fillna(0.0)
    prev = v.shift(1)
    r = (v - f) / prev - 1.0
    r.iloc[0] = 0.0
    r = r.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return pd.DataFrame({"평가액": v, "순입출금": f, "일수익률": r,
                         "누적": (1 + r).cumprod()})


def index_benchmark(px: pd.DataFrame, shares: pd.Series, *,
                    start=None, end=None, top_n: int = INDEX_N) -> pd.DataFrame:
    """시총 상위 top_n 시총가중 지수 (백테스트의 '지수 벤치마크'와 같은 정의).

    가중치는 start 시점 시총으로 한 번 정하고 고정합니다 — 매일 다시 정하면
    상승한 종목으로 비중이 옮겨가 지수가 실제보다 좋아 보입니다.
    """
    if px.empty or not len(shares):
        return pd.DataFrame(columns=["일수익률", "누적"])
    w = px.loc[:start] if start is not None else px
    w = w.ffill()
    if not len(w):
        return pd.DataFrame(columns=["일수익률", "누적"])
    mc = (w.iloc[-1] * shares.reindex(px.columns)).replace(
        [np.inf, -np.inf], np.nan).dropna()
    if not len(mc):
        return pd.DataFrame(columns=["일수익률", "누적"])
    big = mc.nlargest(min(top_n, len(mc))).index
    sl = px.loc[(px.index >= pd.Timestamp(start)) if start is not None
                else slice(None)]
    if end is not None:
        sl = sl.loc[sl.index <= pd.Timestamp(end)]
    sl = sl[big].ffill()
    if len(sl) < 2:
        return pd.DataFrame({"일수익률": [0.0] * len(sl), "누적": [1.0] * len(sl)},
                            index=sl.index)
    wt = mc.reindex(big)
    wt = wt / wt.sum()
    rets = sl.pct_change(fill_method=None).fillna(0.0)
    r = (rets * wt).sum(axis=1)
    r.iloc[0] = 0.0
    return pd.DataFrame({"일수익률": r, "누적": (1 + r).cumprod()})


# ── 종목별 손익 ─────────────────────────────────────────────────────
def position_pnl(trades: pd.DataFrame, holdings_log: pd.DataFrame,
                 px: pd.DataFrame, account: str, *,
                 cost_basis: pd.Series | None = None) -> pd.DataFrame:
    """지금 들고 있는 종목의 평단·현재가·손익.

    평단은 원장의 체결 기록에서 계산합니다. 원장이 생기기 전부터 들고 있던
    종목은 체결 기록이 없어 평단을 알 수 없습니다 — cost_basis(종목코드 →
    매입단가)를 주면 그걸 씁니다. 둘 다 없으면 '원가미상'으로 남기고 손익을
    비웁니다. 모르는 값을 0이나 현재가로 채우면 전체 손익이 조용히 틀립니다.
    """
    pos = positions_timeline(holdings_log, account, px)
    if pos.empty:
        return pd.DataFrame()
    qty = pos.iloc[-1].dropna()
    qty = qty[qty > 0]
    if not len(qty):
        return pd.DataFrame()

    t = trades[trades["계좌"].astype(str) == str(account)].copy() \
        if not trades.empty else trades
    avg: dict[str, float] = {}
    if len(t):
        p = pd.to_numeric(t.get("체결가"), errors="coerce")
        q = pd.to_numeric(t.get("체결수량"), errors="coerce")
        kind = t["구분"].astype(str)
        buy = (p.notna() & q.notna() & (q > 0)
               & kind.str.contains("|".join(BUY_WORDS), na=False))
        b = t[buy].copy()
        if len(b):
            b["_코드"] = b["종목코드"].astype(str).str.zfill(6)
            b["_금액"] = p[buy].to_numpy() * q[buy].to_numpy()
            b["_수량"] = q[buy].to_numpy()
            g = b.groupby("_코드")[["_금액", "_수량"]].sum()
            avg = (g["_금액"] / g["_수량"]).to_dict()

    last = px.ffill().iloc[-1] if len(px) else pd.Series(dtype="float64")
    names = {}
    if not holdings_log.empty and "종목명" in holdings_log.columns:
        hl = holdings_log[holdings_log["계좌"].astype(str) == str(account)]
        names = dict(zip(hl["종목코드"].astype(str).str.zfill(6),
                         hl["종목명"].astype(str)))

    rows = []
    for code, n in qty.items():
        cur = last.get(code)
        base = avg.get(code)
        if base is None and cost_basis is not None:
            v = cost_basis.get(code)
            base = float(v) if v is not None and np.isfinite(v) else None
         # 원가를 모르면 손익을 비웁니다 — 추정해서 채우지 않습니다.
        pl = ((cur - base) * n if (base and cur and np.isfinite(cur)) else np.nan)
        rows.append({
            "종목코드": code, "종목명": names.get(code, ""), "수량": float(n),
            "평단": base, "현재가": float(cur) if cur and np.isfinite(cur) else np.nan,
            "평가금액": (float(cur) * float(n)
                     if cur and np.isfinite(cur) else np.nan),
            "손익": pl,
            "수익률": (pl / (base * n) if (base and np.isfinite(pl)) else np.nan),
            "원가출처": ("원장" if code in avg else
                     ("입력" if base is not None else "원가미상")),
        })
    out = pd.DataFrame(rows)
    return out.sort_values("손익", ascending=False, na_position="last")


# ── 규칙 준수 ───────────────────────────────────────────────────────
def rule_check(state: pd.DataFrame, trades: pd.DataFrame, account: str, *,
               sector_map: dict | None = None, n_target: int = 100,
               tranches: int = 4) -> dict:
    """설계대로 돌고 있는지 — 종목 수·섹터 쏠림·등분 균형·회전율.

    수익률보다 이게 먼저입니다. 수익률은 운이 섞이지만, 종목 수가 100에서
    70으로 줄거나 한 섹터가 40%가 된 것은 규칙이 깨진 것이고 백테스트가
    보장한 성질이 사라진 상태입니다.
    """
    out: dict = {}
    base = (pd.to_numeric(state["기준금액"], errors="coerce")
            if len(state) else pd.Series(dtype="float64"))
    pending = int((base == 0).sum())
    held = int(len(state) - pending)
    out["보유종목수"] = held
    out["미매수"] = pending
    out["목표종목수"] = n_target
    out["종목수편차"] = held + pending - n_target

    if len(state) and "등분" in state.columns:
        cnt = state["등분"].value_counts().sort_index()
        out["등분별종목수"] = {int(k): int(v) for k, v in cnt.items()}
        exp = (len(state) / max(1, tranches))
        out["등분최대편차"] = (float((cnt - exp).abs().max() / exp)
                          if exp > 0 else 0.0)

    if sector_map and len(state):
        sec = state["종목코드"].astype(str).str.zfill(6).map(
            lambda c: sector_map.get(c, "기타"))
        amt = pd.to_numeric(state["기준금액"], errors="coerce").fillna(0.0)
        tot = float(amt.sum())
        if tot > 0:
            w = amt.groupby(sec.to_numpy()).sum() / tot
            out["섹터비중"] = {str(k): float(v)
                            for k, v in w.sort_values(ascending=False).items()}
            out["최대섹터비중"] = float(w.max())

    if not trades.empty:
        t = trades[trades["계좌"].astype(str) == str(account)].copy()
        if len(t):
            t["기준일"] = pd.to_datetime(t["기준일"])
            p = pd.to_numeric(t.get("체결가"), errors="coerce")
            q = pd.to_numeric(t.get("체결수량"), errors="coerce")
            done = p.notna() & q.notna()
            amt = pd.Series(np.where(done, p * q, np.nan), index=t.index)
            per = amt.groupby(t["기준일"]).sum(min_count=1)
            out["주별체결금액"] = {f"{k:%Y-%m-%d}": float(v)
                              for k, v in per.dropna().items()}
            out["체결건수"] = int(done.sum())
            out["미체결건수"] = int((~done).sum())
    return out


def turnover(trades: pd.DataFrame, value: pd.Series, account: str) -> pd.Series:
    """주별 회전율 = 그 주 체결금액 ÷ 그 주 시작 평가액.

    백테스트의 회전율과 같은 뜻이어야 비교가 됩니다. 분모를 '약정액'으로
    하면 분할 진입 중 회전율이 실제보다 낮게 보입니다.
    """
    if trades.empty or value.empty:
        return pd.Series(dtype="float64")
    t = trades[trades["계좌"].astype(str) == str(account)].copy()
    if t.empty:
        return pd.Series(dtype="float64")
    p = pd.to_numeric(t.get("체결가"), errors="coerce")
    q = pd.to_numeric(t.get("체결수량"), errors="coerce")
    ok = p.notna() & q.notna()
    if not ok.any():
        return pd.Series(dtype="float64")
    when = pd.to_datetime(t.loc[ok, "체결일"]).dt.normalize()
    amt = (p[ok] * q[ok]).abs()
    per = pd.Series(amt.to_numpy(), index=when.to_numpy()).groupby(level=0).sum()
    v = value.sort_index()
    # ffill만 쓰면 평가액 곡선보다 앞선 체결(첫 주 매수)이 NaN이 되어
    # 회전율이 조용히 사라집니다 — bfill로 첫 평가액을 끌어다 씁니다.
    denom = (v.reindex(v.index.union(per.index))
             .ffill().bfill().reindex(per.index))
    return (per / denom).replace([np.inf, -np.inf], np.nan).dropna()


# ── 백테스트 대비 괴리 ──────────────────────────────────────────────
def backtest_percentile(live_return: float, bt_returns: pd.Series,
                        days: int) -> dict:
    """실제 수익률이 백테스트의 같은 길이 구간 수익률 분포에서 몇 분위인가.

    왜 분위인가: 백테스트는 과거이고 실제는 현재라 같은 기간을 겹쳐 비교할 수
    없습니다. 대신 '백테스트에서 N일 구간 수익률이 이랬는데 지금은 그 중
    어디쯤'을 보면, 한 주 수익률이 -3%인 게 정상 범위인지 규칙이 깨진 건지
    구분됩니다. 중앙값 근처면 정상, 하위 5% 밖이면 들여다볼 일입니다.
    """
    r = pd.to_numeric(bt_returns, errors="coerce").dropna()
    if len(r) < days + 1 or days < 1:
        return {}
    cum = (1 + r).cumprod()
    win = (cum.shift(-days) / cum - 1).dropna()        # 겹치는 N일 구간 전체
    if not len(win):
        return {}
    pct = float((win < live_return).mean())
    return {"구간일수": int(days), "실제수익률": float(live_return),
            "백테스트_중앙값": float(win.median()),
            "백테스트_5분위": float(win.quantile(0.05)),
            "백테스트_95분위": float(win.quantile(0.95)),
            "분위": pct, "표본수": int(len(win)),
            "판정": ("정상 범위" if 0.05 <= pct <= 0.95
                   else ("백테스트 하위 5% 밖 — 확인 필요" if pct < 0.05
                         else "백테스트 상위 5% 밖 — 운이 좋았습니다"))}
