# -*- coding: utf-8 -*-
"""실전 분할 리밸런싱 — 이번 주에 뭘 사고 팔지 계산합니다.

왜 필요한가
-----------
백테스트에서 한국장 최적 설정은 '100종목 · 주간 · 4등분'이었습니다.
매주 리밸런싱하되 자금의 1/4만 손대는 방식입니다. 간격은 짧아지는데
회전율은 월간과 같아서(연 211% vs 212%) 증권거래세가 늘지 않습니다.

그런데 모델은 매번 '지금 좋은 100종목' 명단만 내놓습니다. 그걸 그대로
따르면 매주 포트폴리오 전체를 갈아엎게 되고, 그건 백테스트에서 연 634%
회전율로 초과수익 -12%p를 낸 바로 그 방식입니다.

그래서 보유 상태를 기억했다가, 매주 **한 등분만** 손봅니다.

어느 등분을 손볼지는 ISO 주차 % 등분수로 정합니다. 실행을 한 주 건너뛰어도
순서가 꼬이지 않고, 같은 주에 두 번 돌려도 같은 등분을 봅니다.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

import store

log = logging.getLogger("quant_agent.portfolio")

STATE = "kor_portfolio"     # 종목코드 · 등분 · 편입일 · 종목명 · 섹터


def _sizes(n: int, tranches: int) -> list[int]:
    return [n // tranches + (1 if j < n % tranches else 0) for j in range(tranches)]


def seed(holdings, *, tranches: int = 4, today: date | None = None) -> dict:
    """이미 들고 있는 종목을 등분에 나눠 넣습니다 — 전환의 출발점.

    이걸 안 하면 첫 주문서가 '100종목 신규 매수'로 나옵니다. 이미 주식을
    들고 있는데 살 돈이 없으니 실행이 불가능하고, 한 주에 전 재산을 다
    갈아엎는 것도 진입 시점을 하루에 몰아버리는 일입니다.

    등분은 **종목 수가 아니라 금액**이 비슷해지게 나눕니다. 매주 한 등분을
    팔아 그 돈으로 새 종목을 사기 때문에, 금액이 쏠려 있으면 어떤 주에는
    살 돈이 모자라고 어떤 주에는 남습니다.

    holdings: [{"종목코드": "005930", "평가금액": 3_200_000}, ...]
              또는 같은 컬럼을 가진 DataFrame.
    """
    today = today or date.today()
    tranches = max(1, int(tranches))

    df = pd.DataFrame(holdings).copy()
    if df.empty:
        raise ValueError("보유 종목이 비어 있습니다.")
    for col in ("종목코드", "평가금액"):
        if col not in df.columns:
            raise ValueError(f"'{col}' 컬럼이 필요합니다 (받은 컬럼: {list(df.columns)})")
    df["종목코드"] = df["종목코드"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6)
    df["평가금액"] = pd.to_numeric(df["평가금액"], errors="coerce").fillna(0.0)
    df = df[df["평가금액"] > 0].drop_duplicates("종목코드")
    if df.empty:
        raise ValueError("평가금액이 0보다 큰 보유 종목이 없습니다.")

    # 큰 종목부터 '지금 가장 가벼운 등분'에 넣습니다. 금액 균형을 맞추는
    # 가장 단순한 방법이고, 종목 수가 적을 때도 최선에 가깝게 나뉩니다.
    df = df.sort_values("평가금액", ascending=False)
    totals = [0.0] * tranches
    rows = []
    for _, r in df.iterrows():
        j = int(min(range(tranches), key=lambda k: totals[k]))
        totals[j] += float(r["평가금액"])
        rows.append({"종목코드": r["종목코드"], "등분": j, "편입일": today,
                     "종목명": str(r.get("종목명", "") or ""), "섹터": ""})

    state = pd.DataFrame(rows)
    state["편입일"] = pd.to_datetime(state["편입일"])
    store.write(STATE, state)

    total = float(df["평가금액"].sum())
    share = [round(v / total, 4) if total else 0.0 for v in totals]
    log.info("보유 %d종목을 %d등분에 배분 — 등분별 금액비중 %s",
             len(state), tranches, share)
    return {"보유종목수": len(state), "등분수": tranches, "총평가액": total,
            "등분별금액": [round(v) for v in totals], "등분별비중": share,
            # 금액이 고르지 않으면 어떤 주에는 살 돈이 모자랍니다.
            "금액균형경고": (None if len(df) >= tranches * 2 and max(share) <= 0.40
                        else f"등분별 금액이 고르지 않습니다(최대 {max(share):.0%}). "
                             f"보유 종목이 {len(df)}개뿐이라 한 주에 팔 돈과 살 돈이 "
                             f"어긋날 수 있습니다 — 모자라면 그 주는 살 수 있는 "
                             f"만큼만 사고 다음 차례에 채우세요.")}


def _affordable(codes, price_map, unit_krw: float):
    """1주 가격이 종목당 배정액보다 비싼 종목을 걸러냅니다 (한국 주식은 1주 단위)."""
    if unit_krw <= 0 or price_map is None:
        return list(codes), []
    ok, over = [], []
    for c in codes:
        px = price_map.get(c)
        if px is None or not np.isfinite(px) or px <= 0:
            ok.append(c)          # 주가를 모르면 통과시키고 수량 칸을 비웁니다
        elif float(px) <= unit_krw:
            ok.append(c)
        else:
            over.append(c)
    return ok, over


def load_state() -> pd.DataFrame:
    df = store.read(STATE)
    if df.empty:
        return pd.DataFrame(columns=["종목코드", "등분", "편입일", "종목명", "섹터"])
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    return df


def current_slot(tranches: int, today: date | None = None) -> int:
    """이번에 손볼 등분 번호. ISO 주차 기준이라 한 주 걸러도 안 꼬입니다."""
    today = today or date.today()
    return int(today.isocalendar()[1]) % max(1, tranches)


def rebalance(picks: pd.DataFrame, *, n: int = 100, tranches: int = 4,
              today: date | None = None, symbol: str = "종목코드",
              name: str = "종목명", sector: str = "SEC_NM_KOR",
              capital: float = 0.0, close: str = "종가") -> dict:
    """모델 순위표(picks)를 받아 이번 회차 주문을 계산하고 상태를 갱신합니다.

    picks는 qvm 오름차순(좋은 종목이 위)으로 정렬돼 있어야 합니다.

    capital > 0 이면 종목당 배정액(capital ÷ n)으로 매수 수량을 계산하고,
    1주 가격이 배정액보다 비싼 종목은 후보에서 빼고 다음 순위로 채웁니다.
    """
    today = today or date.today()
    tranches = max(1, int(tranches))
    sizes = _sizes(n, tranches)

    picks = picks.copy()
    picks[symbol] = picks[symbol].astype(str).str.zfill(6)
    info = picks.drop_duplicates(symbol).set_index(symbol)

    unit = (float(capital) / max(n, 1)) if capital and capital > 0 else 0.0
    price_map = None
    if close in info.columns:
        price_map = {str(k): float(v) for k, v in
                     pd.to_numeric(info[close], errors="coerce").items()
                     if pd.notna(v)}

    ranked = picks[symbol].tolist()
    ranked, too_pricey = _affordable(ranked, price_map, unit)
    target = ranked[:max(n * 2, n + sum(sizes))]

    state = load_state()
    first = state.empty

    if first:
        # 첫 실행 — 전액을 한 번에 넣습니다. 순위를 등분에 번갈아 나눠
        # 어느 한 등분만 상위권을 독차지하지 않게 합니다.
        rows, slot_used = [], None
        for i, code in enumerate(target[:n]):
            rows.append({"종목코드": code, "등분": i % tranches, "편입일": today})
        new_state = pd.DataFrame(rows)
        buys, sells, keeps = [r["종목코드"] for r in rows], [], []
    else:
        slot_used = current_slot(tranches, today)
        mine = state[state["등분"] == slot_used]["종목코드"].tolist()
        others = state[state["등분"] != slot_used]["종목코드"].tolist()
        tset = set(target[:n])

        keeps = [c for c in mine if c in tset]          # 아직 상위면 그대로
        sells = [c for c in mine if c not in tset]
        blocked = set(keeps) | set(others)
        need = sizes[slot_used] - len(keeps)
        buys = [c for c in target if c not in blocked][:max(need, 0)]

        kept = state[state["등분"] != slot_used]
        rows = [{"종목코드": c, "등분": slot_used,
                 "편입일": state.loc[state["종목코드"] == c, "편입일"].iloc[0]}
                for c in keeps]
        rows += [{"종목코드": c, "등분": slot_used, "편입일": today} for c in buys]
        new_state = pd.concat([kept[["종목코드", "등분", "편입일"]],
                               pd.DataFrame(rows)], ignore_index=True)

    # 이름·섹터를 붙여 저장 (사람이 읽을 수 있게)
    new_state["종목명"] = new_state["종목코드"].map(
        info[name] if name in info.columns else {})
    new_state["섹터"] = new_state["종목코드"].map(
        info[sector] if sector in info.columns else {})
    new_state["편입일"] = pd.to_datetime(new_state["편입일"])
    store.write(STATE, new_state)

    def rows_for(codes, kind):
        out = []
        for c in codes:
            r = info.loc[c] if c in info.index else None
            px = (price_map or {}).get(c)
            # 매수는 배정액 ÷ 주가를 내림. 매도는 그 종목을 전부 비우므로
            # 수량을 여기서 정하지 않습니다 — 보유 수량은 증권사 앱에 있습니다.
            qty = None
            if kind == "매수" and unit > 0 and px and px > 0:
                qty = int(unit // px)
            out.append({"구분": kind, "종목코드": c,
                        "종목명": (r[name] if r is not None and name in info.columns else ""),
                        "섹터": (r[sector] if r is not None and sector in info.columns else ""),
                        "주가": (round(px) if px else None),
                        "수량": ("전량" if kind == "매도" else qty),
                        "예상금액": (round(qty * px) if qty and px else None),
                        "qvm": (round(float(r["qvm"]), 4)
                                if r is not None and "qvm" in info.columns
                                and pd.notna(r["qvm"]) else None)})
        return out

    orders = pd.DataFrame(rows_for(sells, "매도") + rows_for(buys, "매수"))

    return {
        "첫실행": first,
        "이번등분": slot_used,
        "등분수": tranches,
        "목표종목수": n,
        "보유종목수": len(new_state),
        "매도": len(sells), "매수": len(buys), "유지": len(keeps),
        "종목당배정액": round(unit) if unit > 0 else None,
        "가격초과제외": len(too_pricey),
        "orders": orders,
        "holdings": new_state.sort_values(["등분", "종목코드"]).reset_index(drop=True),
    }
