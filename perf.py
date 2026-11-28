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
def _c6(x) -> str:
    """스칼라 하나 — 숫자로만 된 코드에만 앞자리 0을 채웁니다."""
    t = str(x).strip()
    return t.zfill(6) if t.isdigit() else t


def _code6(s) -> pd.Series:
    """종목코드 정규화 — **숫자로만 된 코드에만** 앞자리 0을 채웁니다.

    왜 조건을 붙였나: 예전에는 무조건 `zfill(6)`이었습니다. 한국 종목코드는
    전부 숫자라 문제가 없었지만, 대시보드가 미국 계좌까지 그리게 되면서
    같은 함수에 티커가 들어옵니다. `'AAPL'.zfill(6)`은 `'00AAPL'`이고
    `'KO'`는 `'0000KO'`입니다 — 주가와 종목코드가 안 맞아 평가액 곡선과
    종목별 손익이 **조용히 비어 버립니다**. store._normalize에서 똑같은
    사고를 이미 한 번 냈습니다(2026-10-21, 미국 상태에 '0000KO'가 저장).
    """
    v = pd.Series(s).astype(str).str.strip()
    digits = v.str.fullmatch(r"\d+").fillna(False)
    return v.where(~digits, v.str.zfill(6))


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
    df[code_col] = _code6(df[code_col])
    if codes is not None:
        want = {_c6(c) for c in codes}
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
    code = _code6(t[code_col])
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
    h["종목코드"] = _code6(h["종목코드"])
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
    # **스냅샷에 없는 종목은 0주입니다.** 재동기화는 그 날 보유의 전체를
    # 적으므로, 어떤 종목이 그 스냅샷에 없다는 것은 '모른다'가 아니라
    # '팔아서 없다'는 뜻입니다.
    #
    # 왜 이 한 줄이 중요한가 (2026-11-11 실측): 비워 두면 daily_value의
    # ffill이 **직전 스냅샷의 수량을 그대로 끌고 옵니다.** 그러면 팔아서
    # 없어진 종목이 평가액에 계속 남습니다. 미국 main이 10/31 58종목에서
    # 11/04 54종목으로 바뀔 때 팔린 30종목($10,667)이 11/04 평가액에
    # 그대로 남아, 실제 $12,165인 계좌가 $22,832로 계산됐고 7일 수익률이
    # **+53%** 로 찍혔습니다(실제는 -0.8%). 한국도 같은 코드라 매주 팔린
    # 종목이 조금씩 쌓여 수익률을 부풀리고 있었습니다.
    return out.fillna(0.0)


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


def align_flows(flows: pd.Series, value_index, snapshots) -> pd.Series:
    """현금흐름을 **평가액 곡선이 그 거래를 알게 되는 날**로 옮깁니다.

    왜 필요한가 (2026-10-22 실측): 금요일 10/16에 체결하고 일요일 10/18에
    재동기화를 돌렸습니다. 평가액 곡선은 스냅샷에서만 보유 수량이 바뀌는
    계단이라, 새로 산 종목이 처음 나타나는 날은 10/18 다음 거래일인
    **10/19(월)** 입니다. 그런데 현금흐름은 실제 체결일인 10/16에 찍힙니다.
    하루가 어긋나서, 10/19의 평가액 점프 6,231,798원이 입출금으로 설명되지
    않고 **수익으로** 계산됐습니다.

    체결일을 틀리게 적는 것(그건 따로 고쳤습니다)과는 다른 문제입니다.
    둘 다 맞는데 **측정 시점이 다른** 것이고, 둘을 같은 시계에 올려야
    서로 상쇄됩니다.

    규칙: 체결일 f 이후(같은 날 포함) 첫 스냅샷 s를 찾고, s 이후 첫
    거래일에 그 현금흐름을 놓습니다. f 뒤에 스냅샷이 없으면 **버립니다** —
    평가액 곡선이 아직 그 거래를 모르므로(옛 수량으로 계산 중), 현금흐름만
    넣으면 없는 손실이 생깁니다.
    """
    out = pd.Series(dtype="float64")
    if flows is None or not len(flows):
        return out
    idx = pd.DatetimeIndex(value_index).sort_values()
    snap = pd.DatetimeIndex(pd.Index(snapshots)).sort_values()
    if not len(idx) or not len(snap):
        return out
    acc: dict = {}
    for f, amt in flows.items():
        f = pd.Timestamp(f).normalize()
        j = snap.searchsorted(f, side="left")       # f 이후 첫 스냅샷
        if j >= len(snap):
            continue                                # 아직 반영 안 된 거래
        k = idx.searchsorted(snap[j], side="left")  # 그 뒤 첫 거래일
        if k >= len(idx):
            continue
        d = idx[k]
        acc[d] = acc.get(d, 0.0) + float(amt)
    if not acc:
        return out
    return pd.Series(acc).sort_index()


# ── 퀀트로 산 종목만 ─────────────────────────────────────────────
# 대시보드의 수익률은 '퀀트가 산 종목'의 성과입니다. 시스템을 쓰기 전부터
# 들고 있던 종목(원장에 '매수'가 없는 종목)은 들고 있는 동안의 등락도,
# 팔 때의 손익도 넣지 않습니다.
#
# 왜 (2026-11-28 실측): 상수 국장이 -7.57%로 나왔는데 증권사 앱은 -2.76%
# 였습니다. 10월에 원래 들고 있던 80종목(2,228만원)을 정리하는 동안 지수가
# 주마다 -11%, -10%씩 빠졌고, 그 손실이 전부 퀀트 성과로 잡혀 있었습니다.
ENTRY_WORDS = ("매수",)            # 신규 편입만 — 비중확대·현금투입은 아님
FILL_PRICE_GAP = 0.5               # 체결가가 그날 종가와 이만큼 넘게 다르면 오타로 봅니다


def entry_dates(trades: pd.DataFrame, account: str) -> dict:
    """종목 → 퀀트가 처음 산 주문 날짜. 원장에 '매수'가 없는 종목은 없음.

    체결 기록이 없는 매수도 셉니다 — 체결내역을 안 올린 주가 있어도 그
    종목이 퀀트 종목이라는 사실은 같습니다. 실제로 안 샀다면 보유목록에
    없으니 평가액에 들어오지 않습니다.
    """
    if trades is None or trades.empty:
        return {}
    t = trades[trades["계좌"].astype(str) == str(account)]
    t = t[t["구분"].astype(str).str.strip().isin(ENTRY_WORDS)]
    if t.empty:
        return {}
    when = pd.to_datetime(t["기준일"], errors="coerce").dt.normalize()
    codes = _code6(t["종목코드"])
    out = when.groupby(codes.to_numpy()).min().dropna()
    return {str(k): pd.Timestamp(v) for k, v in out.items()}


def sleeve_positions(pos: pd.DataFrame, entries: dict) -> pd.DataFrame:
    """보유수량 표에서 퀀트 종목만, 처음 산 날부터 남깁니다."""
    if pos is None or pos.empty:
        return pd.DataFrame()
    keep = [c for c in pos.columns if c in entries]
    out = pos[keep].copy()
    for c in keep:
        out.loc[out.index < entries[c], c] = 0.0
    return out


def sleeve_flows(pos_q: pd.DataFrame, trades: pd.DataFrame, account: str,
                 px: pd.DataFrame, value_index) -> pd.Series:
    """스냅샷 사이 보유수량 변화로 현금흐름을 만듭니다.

    수량이 늘거나 준 만큼을 그 사이 체결가로 셉니다. 체결 기록이 없거나
    맞지 않으면(방향이 반대, 수량이 더 많음, 체결가가 그날 종가와 50% 넘게
    다름) **그 스냅샷 날의 종가**로 셉니다 — 그 거래로는 손익을 만들지
    않는다는 뜻입니다. 체결내역을 빠뜨리거나 오타가 나도 수익률이 튀지
    않습니다 (2026-11-28 실측: 은진 체결내역의 뉴파워프라즈마 체결단가가
    9,100원 대신 91,000원으로 적혀 -813,400원 손실로 잡혔습니다).

    현금흐름은 그 스냅샷이 평가액에 반영되는 첫 거래일에 놓습니다
    (align_flows와 같은 규칙).
    """
    out = pd.Series(dtype="float64")
    if pos_q is None or pos_q.empty or len(pos_q) < 2 or px is None or px.empty:
        return out
    idx = pd.DatetimeIndex(value_index).sort_values()
    if not len(idx):
        return out

    fills = pd.DataFrame()
    if trades is not None and not trades.empty:
        t = trades[trades["계좌"].astype(str) == str(account)].copy()
        pxf = pd.to_numeric(t.get("체결가"), errors="coerce")
        qf = pd.to_numeric(t.get("체결수량"), errors="coerce")
        ok = pxf.notna() & qf.notna() & (qf > 0)
        t = t[ok]
        if len(t):
            sign = np.where(t["구분"].astype(str).str.contains(
                "|".join(SELL_WORDS), na=False), -1.0, 1.0)
            fills = pd.DataFrame({
                "c": _code6(t["종목코드"]).to_numpy(),
                "d": pd.to_datetime(t["체결일"], errors="coerce").dt.normalize().to_numpy(),
                "q": sign * qf[ok].to_numpy(),
                "p": pxf[ok].to_numpy()})

    ff = px.ffill()
    snaps = pos_q.index.sort_values()
    acc: dict = {}
    for prev, s in zip(snaps[:-1], snaps[1:]):
        k = idx.searchsorted(s, side="left")
        if k >= len(idx):
            continue                          # 평가액이 아직 모르는 스냅샷
        d = idx[k]
        upto = ff.loc[:s]
        if not len(upto):
            continue
        close = upto.iloc[-1]
        dq = pos_q.loc[s] - pos_q.loc[prev]
        for c, q in dq[dq.abs() > 1e-9].items():
            ref = close.get(c)
            if ref is None or not np.isfinite(ref) or ref <= 0:
                continue                      # 가격을 모르면 평가액에도 없습니다
            amt, fq = 0.0, 0.0
            if len(fills):
                f = fills[(fills["c"] == c) & (fills["d"] > prev) & (fills["d"] <= s)]
                f = f[(f["p"] / ref - 1).abs() <= FILL_PRICE_GAP]
                if len(f):
                    nq = float(f["q"].sum())
                    if np.sign(nq) == np.sign(q) and abs(nq) <= abs(q) + 1e-9:
                        fq, amt = nq, float((f["q"] * f["p"]).sum())
            amt += (q - fq) * float(ref)
            acc[d] = acc.get(d, 0.0) + amt
    if not acc:
        return out
    return pd.Series(acc).sort_index()


def sleeve_twr(pos: pd.DataFrame, trades: pd.DataFrame, account: str,
               px: pd.DataFrame) -> pd.DataFrame:
    """퀀트로 산 종목만의 시간가중수익률. twr()와 같은 열."""
    pos_q = sleeve_positions(pos, entry_dates(trades, account))
    if pos_q.empty:
        return twr(pd.Series(dtype="float64"))
    value = daily_value(pos_q, px)
    value = value[value.cumsum() > 0]         # 첫 퀀트 종목이 들어온 날부터
    if value.empty:
        return twr(value)
    flows = sleeve_flows(pos_q, trades, account, px, value.index)
    return twr(value, flows)


def twr_trust(t: pd.DataFrame, *, band: float = 0.08) -> dict:
    """이 시간가중수익률을 믿어도 되는가.

    TWR은 `(평가액 - 그날 입출금) / 어제 평가액`입니다. 입출금이 원장에
    안 들어와 있으면 **주식을 산 것이 수익으로 계산됩니다.** 2026-10-21
    실측: 분할 진입 중이던 배우자 계좌가 9일에 +93.57%로 찍혔는데 실제
    손익은 -145,625원이었습니다. 체결내역의 체결일이 '파일을 넣은 날'로
    찍혀 측정 구간 밖으로 빠져 있었던 탓입니다.

    그래서 숫자를 내기 전에 되묻습니다 — 하루 평가액이 band(기본 8%)보다
    크게 움직였는데 그날 입출금이 0이면, 설명되지 않은 점프입니다. 하루에
    8%는 100종목 포트폴리오가 주가만으로 움직이기 어려운 폭입니다.

    돌려주는 것: {"신뢰": bool, "점프일수": n, "점프합": 금액, "날짜": [Timestamp],
                 "사유": str}
    """
    out = {"신뢰": True, "점프일수": 0, "점프합": 0.0, "날짜": [], "사유": ""}
    if t is None or t.empty or len(t) < 2:
        out["신뢰"] = False
        out["사유"] = "거래일이 2일 미만 — 아직 재질 수 없습니다"
        return out
    v = pd.to_numeric(t["평가액"], errors="coerce")
    f = pd.to_numeric(t["순입출금"], errors="coerce").fillna(0.0)
    prev = v.shift(1)

    # **잔차로 판정합니다.** 평가액 변화에서 그날 현금흐름을 걷어낸 나머지가
    # 그날의 진짜 수익입니다. 그게 하루 band(기본 8%)를 넘으면 설명이 안 되는
    # 것입니다 — 100종목 포트폴리오가 주가만으로 하루 8%를 움직이기는
    # 어렵습니다.
    #
    # 예전에는 `(점프가 큰데) 흐름이 작으면` 의심했습니다. 그래서 **흐름이
    # 크면 무조건 통과**시켰는데, 흐름이 큰 것 자체가 거짓 수익의 재료였습니다
    # (2026-11-11 실측: 11/04에 평가액 +3,236인데 흐름 -7,311 → 잔차
    # +10,547 = 직전 평가액의 54%. 예전 판정은 "흐름이 크니 설명됨"으로
    # 통과시켜 +53%를 그대로 내보냈습니다). 방향과 크기를 같이 봐야 합니다.
    resid = (v - prev - f)
    unexplained = (resid.abs() > prev.abs() * band).fillna(False)
    n = int(unexplained.sum())
    out["점프일수"] = n
    out["점프합"] = float(resid[unexplained].abs().sum())
    out["날짜"] = list(t.index[unexplained])
    if n:
        out["신뢰"] = False
        days = ", ".join(f"{d:%m/%d}" for d in t.index[unexplained][:4])
        out["사유"] = (
            f"현금흐름으로 설명되지 않는 평가액 변화 {n}일 ({days}"
            f"{' …' if n > 4 else ''}, 합 {out['점프합']:,.0f}). "
            f"체결내역이 원장에 안 들어왔거나, 체결일이 틀렸거나, 보유목록이 "
            f"일부만 올라왔을 때 생깁니다 — 그만큼이 수익으로 계산됩니다.")
    return out


def trust_brief(trust: dict | None) -> str:
    """twr_trust 결과를 표 한 칸에 들어갈 길이로 줄입니다.

    요약 표의 칸은 좁아서 twr_trust의 '사유' 전문이 들어가지 않습니다.
    그렇다고 사유를 빼면 '측정 전'과 구분이 안 됩니다(2026-11-13 실측:
    배우자 계좌가 입출금누락으로 숨겨졌는데 표에는 '측정 전'으로만
    보였습니다). 날짜와 금액만 남깁니다 — 무엇을 고칠지는 그걸로 압니다.
    """
    if not trust or trust.get("신뢰", True):
        return ""
    days = trust.get("날짜") or []
    if not days:
        return str(trust.get("사유") or "")[:40]
    shown = ", ".join(f"{pd.Timestamp(d):%m/%d}" for d in days[:3])
    more = f" 외 {len(days) - 3}일" if len(days) > 3 else ""
    return (f"설명 안 되는 평가액 변화 {shown}{more} "
            f"(합 {float(trust.get('점프합') or 0):,.0f})")


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
            b["_코드"] = _code6(b["종목코드"])
            b["_금액"] = p[buy].to_numpy() * q[buy].to_numpy()
            b["_수량"] = q[buy].to_numpy()
            g = b.groupby("_코드")[["_금액", "_수량"]].sum()
            avg = (g["_금액"] / g["_수량"]).to_dict()

    last = px.ffill().iloc[-1] if len(px) else pd.Series(dtype="float64")
    names = {}
    if not holdings_log.empty and "종목명" in holdings_log.columns:
        hl = holdings_log[holdings_log["계좌"].astype(str) == str(account)]
        names = dict(zip(_code6(hl["종목코드"]),
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


def ledger_pnl(trades: pd.DataFrame, account: str, px: pd.DataFrame, *,
               cost_basis: pd.Series | None = None) -> dict:
    """원장만으로 실현·미실현 손익을 셉니다 — **예수금을 몰라도 계산됩니다.**

    종목별로 체결 기록을 시간순으로 걸으며 평단과 수량을 유지하고, 팔 때마다
    (매도가 - 평단) × 수량을 실현손익으로 적립합니다. 남은 수량은 마지막
    종가로 미실현손익을 냅니다. 평가액 스냅샷도, 현금흐름 정렬도 필요 없어서
    (보유목록이 늦게 올라오거나 예수금 줄이 없어도) 흔들리지 않습니다.

    **원가미상은 추정하지 않습니다.** 원장에 매입 기록이 없는 종목을 판
    경우(원장이 생기기 전부터 들고 있던 종목)는 분자·분모 모두에서 빼고,
    몇 종목·얼마를 팔았는지만 돌려줍니다. 모르는 매입가를 0이나 매도가로
    채우면 전체 손익이 조용히 틀립니다. cost_basis(종목코드 → 매입단가)를
    주면 그걸 씁니다.

    돌려주는 것
        실현손익 / 미실현손익 / 손익 (= 둘의 합)
        원가미상_매도종목 · 원가미상_매도금액 · 원가미상_보유종목
        종목별: 종목코드·종목명·상태(보유/정리)·실현손익·미실현손익·손익…
    """
    out = {"실현손익": 0.0, "미실현손익": 0.0, "손익": 0.0,
           "원가미상_매도종목": 0, "원가미상_매도금액": 0.0,
           "원가미상_보유종목": 0, "종목별": pd.DataFrame()}
    if trades is None or trades.empty:
        return out
    t = trades[trades["계좌"].astype(str) == str(account)].copy()
    if t.empty:
        return out
    p = pd.to_numeric(t.get("체결가"), errors="coerce")
    q = pd.to_numeric(t.get("체결수량"), errors="coerce")
    ok = p.notna() & q.notna() & (q > 0)
    t, p, q = t[ok], p[ok], q[ok]
    if t.empty:
        return out
    t["_가"] = p.to_numpy()
    t["_수"] = q.to_numpy()
    t["_코드"] = _code6(t["종목코드"])
    t["_일"] = pd.to_datetime(t.get("체결일"), errors="coerce")
    kind = t["구분"].astype(str)
    t["_매도"] = kind.str.contains("|".join(SELL_WORDS), na=False)
    t["_매수"] = kind.str.contains("|".join(BUY_WORDS), na=False)
    t = t[t["_매도"] | t["_매수"]]
    if t.empty:
        return out
    # 같은 날 매도와 매수가 섞여 있으면 **매도를 먼저** 봅니다. 리밸런싱은
    # 판 돈으로 사는 것이고, 매수를 먼저 보면 그날 평단이 희석된 채로
    # 실현손익이 계산됩니다.
    t = t.sort_values(["_일", "_매도"], ascending=[True, False],
                      kind="stable")

    last = (px.ffill().iloc[-1] if px is not None and len(px)
            else pd.Series(dtype="float64"))
    cb = (cost_basis if cost_basis is not None else pd.Series(dtype="float64"))
    names = {}
    for c, n in zip(t["_코드"], t["종목명"].astype(str)):
        if n and n != "nan":
            names.setdefault(c, n)

    rows = []
    for code, g in t.groupby("_코드", sort=True):
        qty = 0.0
        cost = 0.0          # 보유분의 매입원가 합
        real = 0.0
        sold_amt = 0.0
        unk_qty = 0.0
        unk_amt = 0.0
        for _, r in g.iterrows():
            px_, n_ = float(r["_가"]), float(r["_수"])
            if r["_매도"]:
                if qty <= 0 and code in cb.index and pd.notna(cb.get(code)):
                    # 매입단가를 따로 주셨으면 그걸로 원가를 세웁니다
                    qty, cost = n_, float(cb[code]) * n_
                avail = min(n_, qty)
                if avail > 0:
                    avg = cost / qty
                    real += (px_ - avg) * avail
                    cost -= avg * avail
                    qty -= avail
                    sold_amt += px_ * avail
                if n_ - avail > 1e-9:
                    unk_qty += n_ - avail
                    unk_amt += px_ * (n_ - avail)
            else:
                qty += n_
                cost += px_ * n_
        cur = last.get(code)
        avg = (cost / qty) if qty > 0 else float("nan")
        unreal = ((float(cur) - avg) * qty
                  if qty > 0 and cur is not None and pd.notna(cur)
                  and pd.notna(avg) else float("nan"))
        rows.append({
            "종목코드": code, "종목명": names.get(code, ""),
            "상태": "보유" if qty > 0 else "정리",
            "보유수량": qty, "평단": avg,
            "현재가": float(cur) if cur is not None and pd.notna(cur) else float("nan"),
            "실현손익": real if (real or sold_amt) else float("nan"),
            "미실현손익": unreal,
            "손익": (0.0 if pd.isna(real) else real)
                  + (0.0 if pd.isna(unreal) else unreal),
            "매도금액": sold_amt,
            "원가미상_매도수량": unk_qty, "원가미상_매도금액": unk_amt,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return out
    # 원가미상 매도만 있는 종목은 손익을 0으로 두지 않고 비웁니다
    only_unk = (df["원가미상_매도수량"] > 0) & (df["매도금액"] <= 0) & \
               (df["보유수량"] <= 0)
    df.loc[only_unk, "손익"] = float("nan")
    out["실현손익"] = float(df["실현손익"].sum(skipna=True))
    out["미실현손익"] = float(df["미실현손익"].sum(skipna=True))
    out["손익"] = out["실현손익"] + out["미실현손익"]
    out["원가미상_매도종목"] = int((df["원가미상_매도수량"] > 0).sum())
    out["원가미상_매도금액"] = float(df["원가미상_매도금액"].sum())
    out["원가미상_보유종목"] = int(((df["보유수량"] > 0)
                              & df["평단"].isna()).sum())
    out["종목별"] = df.sort_values("손익", ascending=False,
                                na_position="last").reset_index(drop=True)
    return out


def cost_basis_return(pnl: pd.DataFrame) -> dict:
    """평단 기준 수익률 = 합계 손익 ÷ 합계 투자원금.

    왜 이것도 필요한가: 시간가중수익률(TWR)은 **거래일 두 개**가 있어야
    나옵니다. 첫 주에는 재동기화가 한 번뿐이라 아무 숫자도 못 보여주는데,
    평단과 현재가는 첫날부터 있으니 이건 바로 계산됩니다. 주가가 갱신될
    때마다 매일 바뀝니다.

    TWR과 다른 측정입니다 — 입금 시점을 고려하지 않고 '산 값 대비 지금'만
    봅니다. 그래서 화면에서 둘을 섞지 않고 어느 쪽인지 밝혀 적습니다.
    원가를 모르는 종목은 분모·분자 모두에서 빠집니다.
    """
    if pnl is None or pnl.empty or "손익" not in pnl.columns:
        return {}
    ok = pnl[pnl["손익"].notna()]
    if ok.empty:
        return {}
    원금 = float((ok["평단"] * ok["수량"]).sum())
    손익 = float(ok["손익"].sum())
    if 원금 <= 0:
        return {}
    return {"원금": 원금, "손익": 손익, "수익률": 손익 / 원금,
            "종목수": int(len(ok)), "이긴종목": int((ok["손익"] > 0).sum())}


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
        sec = _code6(state["종목코드"]).map(
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


# ── 배틀 — 두 계좌 맞대결 ────────────────────────────────────
# 대시보드의 '수익률 배틀' 화면이 쓰는 계산입니다. 화면 코드(app.py)에 두지
# 않는 이유: 여기 있어야 오프라인 점검(tools/test_battle.py)으로 숫자를
# 검증할 수 있고, fresh_perf()의 다시 읽기 덕에 배포 뒤 옛 코드가 남지
# 않습니다. 입력은 모두 twr()['누적'] 같은 **1.0에서 시작하는 누적 배수**
# 입니다 — 평단 기준 수익률과 섞어 넣으면 비교 자체가 거짓이 됩니다.

def head_to_head(a: pd.Series, b: pd.Series, *, eps: float = 1e-9) -> dict:
    """누적 곡선 두 개를 날짜별로 맞대 봅니다 (공통 거래일만).

    돌려주는 것:
      격차      Series — (a - b) × 100, 단위 %p. 양수면 a가 앞섬
      앞선날    {"a": n, "b": n} — 동률인 날은 어느 쪽에도 안 셉니다
      역전      리드가 넘어간 횟수 (동률을 지나간 경우도 한 번)
      연속      {"누구": "a" | "b" | None, "일수": n} — 지금 리드가 며칠째인지
    """
    out = {"격차": pd.Series(dtype="float64"), "앞선날": {"a": 0, "b": 0},
           "역전": 0, "연속": {"누구": None, "일수": 0}}
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return out
    idx = a.dropna().index.intersection(b.dropna().index).sort_values()
    if len(idx) == 0:
        return out
    gap = (a.reindex(idx) - b.reindex(idx)) * 100.0
    out["격차"] = gap
    side = pd.Series(np.where(gap > eps * 100, 1,
                              np.where(gap < -eps * 100, -1, 0)), index=idx)
    out["앞선날"] = {"a": int((side == 1).sum()), "b": int((side == -1).sum())}
    led = side[side != 0]
    out["역전"] = int((led != led.shift(1)).sum() - 1) if len(led) else 0
    if len(side) and side.iloc[-1] != 0:
        last = side.iloc[-1]
        n = 0
        for v in side.iloc[::-1]:
            if v != last:
                break
            n += 1
        out["연속"] = {"누구": "a" if last == 1 else "b", "일수": n}
    return out


def weekly_rounds(curves: dict, *, accounts: tuple = ("a", "b")) -> pd.DataFrame:
    """주 단위(금요일 마감) 라운드 — 각 곡선의 그 주 수익률과 승자.

    curves: {키: 누적 배수 Series}. accounts에 든 두 키끼리만 승부를 가리고,
    나머지(지수 등)는 참고로 같은 주 수익률만 붙입니다.
    첫 주는 측정 시작일 값(1.0)을 기준으로 잽니다. 마지막 주가 금요일 전에
    끝났으면 '진행중'으로 표시합니다 — 아직 결판나지 않은 라운드입니다.
    """
    ka, kb = accounts
    if ka not in curves or kb not in curves:
        return pd.DataFrame()
    idx = (curves[ka].dropna().index
           .intersection(curves[kb].dropna().index).sort_values())
    if len(idx) < 2:
        return pd.DataFrame()
    cols = {}
    for k, s in curves.items():
        s = s.dropna().sort_index()
        if k not in (ka, kb):
            s = s.reindex(idx).ffill()
        else:
            s = s.reindex(idx)
        if s.dropna().empty:
            continue
        wk = s.resample("W-FRI").last()
        prev = wk.shift(1)
        prev.iloc[0] = s.dropna().iloc[0]
        cols[k] = wk / prev - 1.0
    df = pd.DataFrame(cols)
    df.index.name = "주말"
    starts = pd.Series(idx, index=idx).resample("W-FRI").first()
    ends = pd.Series(idx, index=idx).resample("W-FRI").last()
    df["시작"], df["끝"] = starts, ends
    df = df.dropna(subset=["끝"])
    diff = df[ka] - df[kb]
    df["승자"] = np.where(diff > 1e-12, ka, np.where(diff < -1e-12, kb, ""))
    df["진행중"] = df.index > idx.max()
    return df


def last_day_moves(px: pd.DataFrame, codes=None) -> pd.Series:
    """종목별 최근 거래일 등락률 (그 종목의 마지막 두 종가).

    가장 최근 날짜에 종가가 없는 종목(거래정지·상장폐지)은 뺍니다 — 며칠
    전 등락을 '오늘'로 띄우면 안 됩니다.
    """
    if px is None or px.empty:
        return pd.Series(dtype="float64")
    p = px.sort_index()
    if codes is not None:
        keep = [c for c in _code6(pd.Series(list(codes))) if c in p.columns]
        p = p[keep]
    last_day = p.index.max()
    out = {}
    for c in p.columns:
        s = p[c].dropna()
        if len(s) >= 2 and s.index[-1] == last_day and s.iloc[-2] > 0:
            out[c] = float(s.iloc[-1] / s.iloc[-2] - 1.0)
    return pd.Series(out, dtype="float64")


def pnl_highlights(pnl: pd.DataFrame, n: int = 3) -> dict:
    """보유 종목 중 많이 오른 것·많이 빠진 것.

    원가를 모르는 종목(원가미상)은 수익률이 없으니 순위에서 빠집니다 —
    추정해서 넣으면 1등이 거짓이 될 수 있습니다.
    """
    out = {"효자": pd.DataFrame(), "아픈": pd.DataFrame(),
           "플러스": 0, "계산됨": 0, "보유": 0}
    if pnl is None or pnl.empty or "수익률" not in pnl.columns:
        return out
    known = pnl[pnl["수익률"].notna()]
    out["보유"] = int(len(pnl))
    out["계산됨"] = int(len(known))
    out["플러스"] = int((known["손익"] > 0).sum())
    out["효자"] = (known[known["수익률"] > 0]
                 .sort_values("수익률", ascending=False).head(n))
    out["아픈"] = (known[known["수익률"] < 0]
                 .sort_values("수익률", ascending=True).head(n))
    return out


# ── 종합 — 국장 + 미국장을 원화로 합치기 ─────────────────────
# 두 시장의 수익률을 그냥 평균내면 안 됩니다 — 넣은 돈의 크기가 다르고,
# 미국 주식은 환율로도 벌거나 잃습니다. 그래서 날마다 '원화 총평가액'을
# 만들고, 입출금도 원화로 바꿔 twr()에 그대로 넣습니다.
#
#     V(t) = V_kr(t) + V_us(t) × 환율(t)
#     F(t) = F_kr(t) + F_us(t) × 환율(t)
#
# 거래일이 다른 날(한쪽만 휴장)은 쉬는 시장의 평가액을 전날 값으로 둡니다
# — 실제로 그 시장 가격이 안 움직였으니까요. 환율은 그날도 움직입니다.

def combine_markets(parts: dict, fx: pd.Series | None,
                    fx_markets: tuple = ("us",)) -> pd.DataFrame:
    """시장별 twr() 결과를 원화로 합친 시간가중수익률.

    parts: {"kr": twr 결과(원화), "us": twr 결과(달러), ...} — 비거나 None인
           시장은 건너뜁니다 (그 사람이 그 시장에 없으면 비중 0).
    fx:    날짜 → 1달러당 원화. fx_markets에 든 시장에만 곱합니다.
    돌려주는 것: twr()와 같은 열 + 시장별 원화 평가액 열 ("평가액_kr" 등).
    환율이 필요한데 없으면 빈 표 — 환율 없이 합치면 단위가 섞입니다.
    """
    use = {m: t for m, t in parts.items() if t is not None and len(t)}
    if not use:
        return pd.DataFrame()
    need_fx = [m for m in use if m in fx_markets]
    if need_fx and (fx is None or fx.dropna().empty):
        return pd.DataFrame()
    idx = pd.DatetimeIndex(sorted(set().union(*[t.index for t in use.values()])))
    x = None
    if need_fx:
        f = fx.dropna().sort_index()
        x = f.reindex(f.index.union(idx)).ffill().bfill().reindex(idx)
    vals, flows = {}, pd.Series(0.0, index=idx)
    for m, t in use.items():
        v = t["평가액"].reindex(idx).ffill().fillna(0.0)
        fl = t["순입출금"].reindex(idx).fillna(0.0)
        # 그 시장이 처음 잡히는 날은 그날 평가액 전체를 '들어온 돈'으로 봅니다
        # — twr()가 첫날 수익을 0으로 두는 것과 같은 약속입니다.
        # 왜 (2026-11-22 실측): 미국 첫 보유(10/16)는 보유목록만 올라오고
        # 체결내역이 없어 입금이 0이었습니다. 미국 탭 혼자는 첫날을 기준점으로
        # 삼으니 괜찮았지만, 합산에서는 미국 주식 전체가 하루 수익으로 잡혀
        # 두 시장 다 마이너스인데 종합이 +67% / +86%로 나왔습니다.
        t0 = t["평가액"].dropna().index.min()
        fl.loc[t0] = v.loc[t0]
        if m in fx_markets:
            v, fl = v * x, fl * x
        vals[f"평가액_{m}"] = v
        flows = flows + fl
    V = pd.DataFrame(vals).sum(axis=1)
    live = V[V > 0]
    if live.empty:
        return pd.DataFrame()
    start = live.index[0]
    out = twr(V.loc[start:], flows.loc[start:])
    for k, v in vals.items():
        out[k] = v.loc[start:]
    if x is not None:
        out["환율"] = x.loc[start:]
    return out


def blended_bench(comb: pd.DataFrame, benches: dict, fx: pd.Series | None = None,
                  fx_markets: tuple = ("us",)) -> pd.Series:
    """'같은 비중 지수' — 그 사람의 시장 비중대로 각 시장 지수를 샀다면.

    comb:    combine_markets() 결과 (평가액_<시장> 열로 전날 비중을 씁니다)
    benches: {"kr": 지수 누적 배수, "us": SPY 누적 배수(달러)}
    하루 수익률 = Σ 전날비중_m × 지수수익률_m (미국 지수는 원화로 환산).
    비중이 0인 시장(아직 안 산 시장)은 그날 영향이 없습니다.
    돌려주는 것: 누적 배수 Series (comb와 같은 날짜, 1.0에서 시작).
    """
    if comb is None or comb.empty:
        return pd.Series(dtype="float64")
    idx = comb.index
    cols = [c for c in comb.columns if c.startswith("평가액_")]
    total = comb[cols].sum(axis=1)
    r = pd.Series(0.0, index=idx)
    for c in cols:
        m = c.split("_", 1)[1]
        b = benches.get(m)
        if b is None or b.dropna().empty:
            continue
        b = b.dropna().sort_index()
        lvl = b.reindex(b.index.union(idx)).ffill().reindex(idx)
        if m in fx_markets and "환율" in comb.columns:
            lvl = lvl * comb["환율"]
        rm = (lvl / lvl.shift(1) - 1.0).fillna(0.0)
        w = (comb[c] / total).shift(1).fillna(0.0)
        r = r + w * rm
    r.iloc[0] = 0.0
    return (1 + r.replace([np.inf, -np.inf], 0.0).fillna(0.0)).cumprod()
