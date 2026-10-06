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

import csv
import logging
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import store

log = logging.getLogger("quant_agent.portfolio")

STATE = "kor_portfolio"     # 종목코드 · 등분 · 편입일 · 종목명 · 섹터


def _sizes(n: int, tranches: int) -> list[int]:
    return [n // tranches + (1 if j < n % tranches else 0) for j in range(tranches)]


# 보유 종목 목록을 사람이 붙여넣은 그대로 받아들이기 위한 준비 ───────────
#
# 증권사 화면이나 엑셀에서 긁어 붙이면 세 가지가 어긋납니다.
#   1. 구분자가 쉼표가 아니라 탭입니다 (엑셀 복사의 기본)
#   2. GitHub Actions 입력창은 줄바꿈을 보존하지 않습니다 — 100줄이 한 줄로
#      뭉쳐서 들어옵니다
#   3. 엑셀이 종목코드의 앞자리 0을 지웁니다 (005930 → 5930, 000070 → 70)
# 이걸 전부 사람에게 고치라고 하는 대신 코드가 받아들입니다.

_HEADER_WORDS = ("종목코드", "코드", "티커", "종목", "평가금액", "금액", "평가",
                 "종목명", "이름", "name", "code", "ticker")

# 줄바꿈이 사라진 한 줄에서 레코드 경계를 찾습니다. 이름 뒤의 공백 다음에
# '숫자 + 구분자'가 오면 거기가 다음 종목의 시작입니다.
_RECORD_SPLIT = re.compile(r" +(?=\d{1,6}[\t,;|])")


def _to_number(v) -> float:
    """'1,234,500원' 같은 값도 숫자로. 못 읽으면 0."""
    if isinstance(v, (int, float)):
        return float(v) if pd.notna(v) else 0.0
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def parse_holdings(src) -> pd.DataFrame:
    """보유 종목 목록을 어떤 모양으로 받아도 종목코드·평가금액·종목명으로.

    받아들이는 것: DataFrame, 레코드 리스트, CSV/TSV 파일 경로, 그리고
    엑셀에서 복사해 붙여넣은 문자열(탭 구분, 줄바꿈이 없어도 됨).
    """
    if isinstance(src, pd.DataFrame):
        df = src.copy()
    elif isinstance(src, (list, tuple)):
        df = pd.DataFrame(list(src))
    else:
        text = str(src)
        # 파일 경로로 들어왔으면 읽어 옵니다. 붙여넣은 내용이 그대로 들어올
        # 수도 있으므로, 경로로 해석할 수 없는 문자열은 조용히 넘깁니다
        # (윈도에서는 Path()가 특수문자에 예외를 던집니다).
        if len(text) < 4096 and "\n" not in text and "\t" not in text:
            try:
                p = Path(text)
                if p.exists():
                    text = p.read_text(encoding="utf-8-sig")
            except (OSError, ValueError):
                pass

        lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
        if len(lines) <= 1:
            # 줄바꿈이 사라진 경우 — 레코드 경계를 되살립니다
            lines = [ln for ln in _RECORD_SPLIT.split(text.strip()) if ln.strip()]

        if "\t" in lines[0]:
            # csv 모듈을 씁니다 — "6,267,500원" 처럼 따옴표 안에 구분자가
            # 들어 있는 칸을 직접 split으로 자르면 숫자가 쪼개집니다.
            parts = list(csv.reader(lines, delimiter="\t"))
        elif "," in lines[0]:
            parts = list(csv.reader(lines))
        else:
            parts = [re.split(r"\s+", ln.strip()) for ln in lines]
        parts = [[c.strip() for c in row] for row in parts if any(c.strip() for c in row)]

        first = parts[0]
        has_header = any(any(w in c.lower() for w in _HEADER_WORDS) for c in first)
        body = parts[1:] if has_header else parts

        cols = ["종목코드", "평가금액", "종목명"]
        if has_header:
            # 헤더가 있으면 순서가 달라도 맞춰 줍니다
            idx = {}
            for i, c in enumerate(first):
                lc = c.lower()
                if any(w in lc for w in ("종목코드", "코드", "티커", "code", "ticker")):
                    idx.setdefault("종목코드", i)
                elif any(w in lc for w in ("평가금액", "금액", "평가")):
                    idx.setdefault("평가금액", i)
                elif any(w in lc for w in ("종목명", "이름", "name")):
                    idx.setdefault("종목명", i)
            if "종목코드" in idx and "평가금액" in idx:
                rows = [{k: (r[i] if i < len(r) else "") for k, i in idx.items()}
                        for r in body]
                df = pd.DataFrame(rows)
            else:
                df = pd.DataFrame(body).iloc[:, :3]
                df.columns = cols[:df.shape[1]]
        else:
            df = pd.DataFrame(body).iloc[:, :3]
            df.columns = cols[:df.shape[1]]

    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    if "종목코드" not in df.columns or "평가금액" not in df.columns:
        raise ValueError(
            "'종목코드'와 '평가금액'을 찾지 못했습니다. "
            f"읽어낸 컬럼: {list(df.columns)}. "
            "엑셀에서 '종목코드 / 평가금액 / 종목명' 세 칸을 복사해 붙여넣으세요.")

    # 엑셀이 지워버린 앞자리 0을 되살립니다 (70 → 000070)
    df["종목코드"] = (df["종목코드"].astype(str)
                   .str.replace(r"\D", "", regex=True).str.zfill(6))
    df["평가금액"] = df["평가금액"].map(_to_number)
    if "종목명" not in df.columns:
        df["종목명"] = ""
    df["종목명"] = df["종목명"].fillna("").astype(str).str.strip()

    df = df[(df["종목코드"].str.len() == 6) & (df["종목코드"] != "000000")]
    df = df[df["평가금액"] > 0].drop_duplicates("종목코드")
    return df[["종목코드", "평가금액", "종목명"]].reset_index(drop=True)


def seed(holdings, *, tranches: int = 4, today: date | None = None) -> dict:
    """이미 들고 있는 종목을 등분에 나눠 넣습니다 — 전환의 출발점.

    이걸 안 하면 첫 주문서가 '100종목 신규 매수'로 나옵니다. 이미 주식을
    들고 있는데 살 돈이 없으니 실행이 불가능하고, 한 주에 전 재산을 다
    갈아엎는 것도 진입 시점을 하루에 몰아버리는 일입니다.

    등분은 **종목 수가 아니라 금액**이 비슷해지게 나눕니다. 매주 한 등분을
    팔아 그 돈으로 새 종목을 사기 때문에, 금액이 쏠려 있으면 어떤 주에는
    살 돈이 모자라고 어떤 주에는 남습니다.

    holdings: parse_holdings가 받아들이는 모든 형태 — DataFrame, 레코드 리스트,
              CSV/TSV 경로, 엑셀에서 복사한 문자열.
    """
    today = today or date.today()
    tranches = max(1, int(tranches))

    df = parse_holdings(holdings)
    if df.empty:
        raise ValueError(
            "읽어낼 보유 종목이 없습니다. 종목코드가 6자리 숫자이고 "
            "평가금액이 0보다 큰 줄이 하나도 없습니다.")

    # 큰 종목부터 '지금 가장 가벼운 등분'에 넣습니다. 금액 균형을 맞추는
    # 가장 단순한 방법이고, 종목 수가 적을 때도 최선에 가깝게 나뉩니다.
    df = df.sort_values("평가금액", ascending=False)
    totals = [0.0] * tranches
    rows = []
    for _, r in df.iterrows():
        j = int(min(range(tranches), key=lambda k: totals[k]))
        totals[j] += float(r["평가금액"])
        rows.append({"종목코드": r["종목코드"], "등분": j, "편입일": today,
                     "기준금액": float(r["평가금액"]),
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
    cols = ["종목코드", "등분", "편입일", "기준금액", "종목명", "섹터"]
    df = store.read(STATE)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    # 기준금액은 나중에 추가된 컬럼입니다 — 예전 파일에는 없습니다.
    if "기준금액" not in df.columns:
        df["기준금액"] = np.nan
    return df


def current_slot(tranches: int, today: date | None = None) -> int:
    """이번에 손볼 등분 번호. ISO 주차 기준이라 한 주 걸러도 안 꼬입니다."""
    today = today or date.today()
    return int(today.isocalendar()[1]) % max(1, tranches)


def rebalance(picks: pd.DataFrame, *, n: int = 100, tranches: int = 4,
              today: date | None = None, symbol: str = "종목코드",
              name: str = "종목명", sector: str = "SEC_NM_KOR",
              capital: float = 0.0, close: str = "종가",
              hold_universe=None) -> dict:
    """모델 순위표(picks)를 받아 이번 회차 주문을 계산하고 상태를 갱신합니다.

    picks는 qvm 오름차순(좋은 종목이 위)으로 정렬돼 있어야 합니다.

    capital > 0 이면 종목당 배정액(capital ÷ n)으로 매수 수량을 계산하고,
    1주 가격이 배정액보다 비싼 종목은 후보에서 빼고 다음 순위로 채웁니다.

    hold_universe를 주면 순위 버퍼로 동작합니다 — 살 때는 picks 상위 n위
    안에서만 고르고, 팔 때는 hold_universe 밖으로 밀려나야 팝니다. 백테스트와
    같은 규칙이어야 하므로 반드시 같은 값을 쓰세요(model_kr_hold.parquet).
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

    trims: list[dict] = []          # 유지하지만 비중을 조절할 종목
    if first:
        # 첫 실행 — 전액을 한 번에 넣습니다. 순위를 등분에 번갈아 나눠
        # 어느 한 등분만 상위권을 독차지하지 않게 합니다.
        rows, slot_used = [], None
        for i, code in enumerate(target[:n]):
            rows.append({"종목코드": code, "등분": i % tranches, "편입일": today,
                         "기준금액": unit or np.nan})
        new_state = pd.DataFrame(rows)
        buys, sells, keeps = [r["종목코드"] for r in rows], [], []
    else:
        slot_used = current_slot(tranches, today)
        mine = state[state["등분"] == slot_used]["종목코드"].tolist()
        others = state[state["등분"] != slot_used]["종목코드"].tolist()
        buy_set = set(target[:n])
        # 유지 판정은 넓은 명단으로 (순위 버퍼). 안 주면 예전처럼 상위 n위.
        hold_set = ({str(c).zfill(6) for c in hold_universe}
                    if hold_universe is not None and len(hold_universe)
                    else buy_set)
        base = dict(zip(state["종목코드"], pd.to_numeric(state["기준금액"],
                                                     errors="coerce")))

        keeps = [c for c in mine if c in hold_set]      # 버퍼 안이면 그대로
        sells = [c for c in mine if c not in hold_set]

        # 목표 비중으로 1주도 담을 수 없는 종목은 들고 있을 수 없습니다.
        # (종목당 30만원인데 1주가 50만원이면 1%를 만들 방법이 없습니다)
        if unit > 0 and price_map:
            unholdable = [c for c in keeps if (price_map.get(c) or 0) > unit]
            if unholdable:
                keeps = [c for c in keeps if c not in set(unholdable)]
                sells += unholdable

        # ── 비중 조절 ────────────────────────────────────────
        # 유지하는 종목이라도 금액이 목표에서 크게 벗어나 있으면 맞춰야
        # 합니다. 이게 없으면 전환이 막힙니다: 삼성전자가 전체의 21%인데
        # '상위 100위라서 유지'로 끝내면, 그 등분을 팔아 나온 돈이 25종목을
        # 사기에 모자랍니다. 넘치는 만큼 덜어내야 새 종목을 살 돈이 생깁니다.
        #
        # ±20% 밴드를 둡니다. 몇 %p 차이로 매번 사고팔면 거래세(0.20%)만
        # 나갑니다.
        if unit > 0 and price_map:
            for c in keeps:
                cur, px = base.get(c), price_map.get(c)
                if not px or px <= 0 or cur is None or not np.isfinite(cur):
                    continue
                gap = cur - unit
                if abs(gap) <= unit * 0.20:
                    continue
                qty = int(abs(gap) // px)
                if qty <= 0:
                    continue
                trims.append({"종목코드": c, "구분": "비중축소" if gap > 0 else "비중확대",
                              "수량": qty, "주가": px,
                              "현재기준": round(cur), "목표": round(unit)})

        # 새로 담는 건 상위 n위에서만 — 버퍼 구간(n위~버퍼)은
        # '들고 있으면 유지, 없으면 안 산다'는 중립 구간입니다.
        blocked = set(keeps) | set(others)
        need = sizes[slot_used] - len(keeps)
        buys = [c for c in target[:n] if c not in blocked][:max(need, 0)]
        if len(buys) < max(need, 0):   # 상위 n위가 모자라면 그 밖에서 보충
            buys += [c for c in target if c not in blocked
                     and c not in set(buys)][:max(need, 0) - len(buys)]

        kept = state[state["등분"] != slot_used]
        # 이번에 손본 등분은 전부 목표 금액에 맞춰졌으므로 기준금액을 갱신합니다.
        # 안 건드린 등분은 예전 기준금액을 그대로 둡니다 — 그 등분 차례가
        # 올 때 다시 맞춥니다.
        rows = [{"종목코드": c, "등분": slot_used,
                 "편입일": state.loc[state["종목코드"] == c, "편입일"].iloc[0],
                 "기준금액": unit if unit > 0 else base.get(c, np.nan)}
                for c in keeps]
        rows += [{"종목코드": c, "등분": slot_used, "편입일": today,
                  "기준금액": unit or np.nan} for c in buys]
        state_cols = ["종목코드", "등분", "편입일", "기준금액"]
        new_state = pd.concat([kept[state_cols],
                               pd.DataFrame(rows, columns=state_cols)],
                              ignore_index=True)

    # 이름·섹터를 붙여 저장 (사람이 읽을 수 있게)
    new_state["종목명"] = new_state["종목코드"].map(
        info[name] if name in info.columns else {})
    new_state["섹터"] = new_state["종목코드"].map(
        info[sector] if sector in info.columns else {})
    new_state["편입일"] = pd.to_datetime(new_state["편입일"])
    store.write(STATE, new_state)

    # 팔 종목은 모델 상위권에서 밀려났으니 picks에 없습니다. 이름을 info에서만
    # 찾으면 매도 줄이 종목코드만 남은 빈 줄이 됩니다 — 22개를 코드만 보고
    # 팔라는 주문서가 됩니다. 보유 상태에 저장해 둔 이름으로 메웁니다.
    held_name = dict(zip(state["종목코드"], state.get("종목명", pd.Series(dtype=str))))
    held_sector = dict(zip(state["종목코드"], state.get("섹터", pd.Series(dtype=str))))

    def _label(c, col, fallback):
        if c in info.index and col in info.columns and pd.notna(info.at[c, col]):
            return info.at[c, col]
        v = fallback.get(c)
        return v if isinstance(v, str) and v.strip() else ""

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
                        "종목명": _label(c, name, held_name),
                        "섹터": _label(c, sector, held_sector),
                        "주가": (round(px) if px else None),
                        "수량": ("전량" if kind == "매도" else qty),
                        "예상금액": (round(qty * px) if qty and px else None),
                        "qvm": (round(float(r["qvm"]), 4)
                                if r is not None and "qvm" in info.columns
                                and pd.notna(r["qvm"]) else None)})
        return out

    def trim_rows():
        out = []
        for t in trims:
            c = t["종목코드"]
            r = info.loc[c] if c in info.index else None
            out.append({"구분": t["구분"], "종목코드": c,
                        "종목명": _label(c, name, held_name),
                        "섹터": _label(c, sector, held_sector),
                        "주가": round(t["주가"]),
                        "수량": t["수량"],
                        "예상금액": round(t["수량"] * t["주가"]),
                        "qvm": (round(float(r["qvm"]), 4)
                                if r is not None and "qvm" in info.columns
                                and pd.notna(r["qvm"]) else None)})
        return out

    ORDER_COLS = ["구분", "종목코드", "종목명", "섹터", "주가", "수량", "예상금액", "qvm"]
    # 거래가 없는 주에도 컬럼은 있어야 합니다. 빈 DataFrame을 그냥 돌려주면
    # 받는 쪽에서 orders["구분"]이 KeyError로 터집니다.
    orders = pd.DataFrame(rows_for(sells, "매도") + trim_rows()
                          + rows_for(buys, "매수"), columns=ORDER_COLS)

    # 이번 주 현금 수지 — 파는 돈으로 사는 돈을 댈 수 있는지.
    # 모자라면 사람이 알아야 합니다. 조용히 주문서를 내밀면 증권사 앱에서
    # 주문이 거부되고, 그게 왜인지 알 수가 없습니다.
    매도대금 = sum(float(t["수량"]) * float(t["주가"])
                for t in trims if t["구분"] == "비중축소")
    매수대금 = sum(float(r["예상금액"] or 0) for r in rows_for(buys, "매수"))
    매수대금 += sum(float(t["수량"]) * float(t["주가"])
                 for t in trims if t["구분"] == "비중확대")

    return {
        "첫실행": first,
        "이번등분": slot_used,
        "등분수": tranches,
        "목표종목수": n,
        "보유종목수": len(new_state),
        "매도": len(sells), "매수": len(buys), "유지": len(keeps),
        "비중조절": len(trims),
        "종목당배정액": round(unit) if unit > 0 else None,
        "가격초과제외": len(too_pricey),
        # 전량매도 종목의 금액은 보유 수량을 몰라 셀 수 없습니다.
        # 그래서 '비중축소로 확보되는 금액'만 셉니다 — 하한입니다.
        "확보금액_하한": round(매도대금),
        "필요금액": round(매수대금),
        "orders": orders,
        "holdings": new_state.sort_values(["등분", "종목코드"]).reset_index(drop=True),
    }
