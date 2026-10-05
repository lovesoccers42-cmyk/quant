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

import pandas as pd

import store

log = logging.getLogger("quant_agent.portfolio")

STATE = "kor_portfolio"     # 종목코드 · 등분 · 편입일 · 종목명 · 섹터


def _sizes(n: int, tranches: int) -> list[int]:
    return [n // tranches + (1 if j < n % tranches else 0) for j in range(tranches)]


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
              name: str = "종목명", sector: str = "SEC_NM_KOR") -> dict:
    """모델 순위표(picks)를 받아 이번 회차 주문을 계산하고 상태를 갱신합니다.

    picks는 qvm 오름차순(좋은 종목이 위)으로 정렬돼 있어야 합니다.
    """
    today = today or date.today()
    tranches = max(1, int(tranches))
    sizes = _sizes(n, tranches)

    picks = picks.copy()
    picks[symbol] = picks[symbol].astype(str).str.zfill(6)
    target = picks[symbol].tolist()[:max(n * 2, n + sum(sizes))]
    info = picks.drop_duplicates(symbol).set_index(symbol)

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
            out.append({"구분": kind, "종목코드": c,
                        "종목명": (r[name] if r is not None and name in info.columns else ""),
                        "섹터": (r[sector] if r is not None and sector in info.columns else ""),
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
        "orders": orders,
        "holdings": new_state.sort_values(["등분", "종목코드"]).reset_index(drop=True),
    }
