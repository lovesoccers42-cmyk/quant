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

# 보유 상태는 프로필마다 다른 테이블에 들어갑니다. 섞이면 한 사람의 매도가
# 다른 사람 주문서에 나타납니다. 그래서 테이블 이름을 인자로 받고, 기본값은
# 두지 않습니다 — 실수로 main 테이블에 alt를 쓰는 일을 막습니다.
STATE = "kor_portfolio"     # main 프로필 (기존 이름 유지)

# 무매매 밴드 — 목표 금액에서 이 비율 안으로 벗어난 건 되돌리지 않습니다.
# 백테스트(config.REBAL_BAND)와 같은 값이어야 합니다. 비중 조절과 현금 투입이
# 같은 숫자를 써야 "밴드 안은 손대지 않는다"는 규칙이 한 가지로 유지됩니다.
BAND = 0.20


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


def seed(holdings, *, tranches: int = 4, today: date | None = None,
         table: str = STATE) -> dict:
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
    store.write(table, state)

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



def target_amounts(ranked, *, capital: float, n: int, weighting: str = "equal",
                   price_map=None, vol_map=None, cap: float = 0.0,
                   rank_map=None, universe_n: int = 0) -> dict:
    """종목별 목표 금액. 백테스트의 비중 방식을 실전 주문서에 그대로 옮깁니다.

    이게 없으면 주문서는 전 종목에 같은 금액을 배정합니다 — 그러면 점수가중을
    검증해 놓고 실제로는 동일가중을 사게 됩니다. 백테스트에서 그 둘의 차이가
    9.5년 누적 +12.75%p였습니다.

    ranked는 점수 좋은 순 전체 명단입니다(버퍼 구간 포함). 순위는 그 전체에서
    매깁니다 — 상위 n위 안에서만 매기면 버퍼 구간 종목이 꼴찌 비중을 받고
    n위 안에 들어오는 순간 비중이 튑니다.
    """
    codes = [str(c).zfill(6) for c in ranked]
    if not codes or capital <= 0:
        return {}
    w = {}
    if weighting == "score":
        # 전체 유니버스 순위가 있으면 그걸 씁니다 (백테스트와 동일).
        # 없으면 받은 명단 안에서의 순서로 물러섭니다.
        if rank_map and universe_n > 0:
            m = float(universe_n)
            for c in codes:
                rk = rank_map.get(c)
                rk = float(rk) if rk is not None and np.isfinite(rk) else m
                w[c] = max(m - rk + 1.0, 1.0)
        else:
            m = len(codes)
            for i, c in enumerate(codes):
                w[c] = float(m - i)
    elif weighting == "invvol":
        vol = vol_map or {}
        vals = [v for v in (vol.get(c) for c in codes)
                if v is not None and np.isfinite(v) and v > 0]
        if len(vals) < max(5, len(codes) // 2):
            w = {c: 1.0 for c in codes}          # 데이터가 모자라면 동일가중
        else:
            med = float(np.median(vals))
            for c in codes:
                v = vol.get(c)
                v = float(v) if (v is not None and np.isfinite(v) and v > 0) else med
                w[c] = 1.0 / v
    else:
        w = {c: 1.0 for c in codes}

    # 상한 적용 후 상위 n종목에 배분
    keep = codes[:n]
    sub = np.array([w[c] for c in keep], dtype="float64")
    sub = sub / sub.sum()
    if 0 < cap < 1 and len(keep) * cap >= 1:
        for _ in range(100):
            over = sub > cap + 1e-12
            if not over.any():
                break
            excess = float((sub[over] - cap).sum())
            sub[over] = cap
            free = ~over
            base_ = float(sub[free].sum())
            if base_ <= 0:
                sub[free] += excess / max(int(free.sum()), 1)
                break
            sub[free] += excess * (sub[free] / base_)
        sub = sub / sub.sum()

    out = {c: float(capital) * float(x) for c, x in zip(keep, sub)}
    # 버퍼 구간 종목도 목표가 있어야 비중 조절을 계산할 수 있습니다.
    # 순위 밖이니 n위 종목의 목표를 그대로 씁니다 (더 싣지는 않음).
    tail = float(min(out.values())) if out else 0.0
    for c in codes[n:]:
        out.setdefault(c, tail)
    return out


def _affordable(codes, price_map, amount_of):
    """1주 가격이 그 종목의 목표 금액보다 비싸면 걸러냅니다 (한국 주식은 1주 단위).

    amount_of는 종목코드 → 목표 금액 함수입니다. 비중 방식에 따라 종목마다
    목표가 다르므로 하나의 '종목당 배정액'으로는 판정할 수 없습니다.
    """
    if price_map is None:
        return list(codes), []
    ok, over = [], []
    for c in codes:
        px, amt = price_map.get(c), amount_of(c)
        if px is None or not np.isfinite(px) or px <= 0 or not amt or amt <= 0:
            ok.append(c)          # 주가나 목표를 모르면 통과 (수량 칸은 비움)
        elif float(px) <= float(amt):
            ok.append(c)
        else:
            over.append(c)
    return ok, over


def load_state(table: str = STATE) -> pd.DataFrame:
    cols = ["종목코드", "등분", "편입일", "기준금액", "종목명", "섹터"]
    df = store.read(table)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    # 기준금액은 나중에 추가된 컬럼입니다 — 예전 파일에는 없습니다.
    if "기준금액" not in df.columns:
        df["기준금액"] = np.nan
    return df


def current_slot(tranches: int, today: date | None = None,
                 offset: int = 0) -> int:
    """이번에 손볼 등분 번호. ISO 주차 기준이라 한 주 걸러도 안 꼬입니다.

    offset은 프로필마다 손보는 등분을 엇갈리게 합니다. 두 계좌가 같은 주에
    같은 소형주를 동시에 사면 서로 호가를 밀어올리기 때문입니다.
    """
    today = today or date.today()
    tr = max(1, tranches)
    return (int(today.isocalendar()[1]) + int(offset)) % tr


def rebalance(picks: pd.DataFrame, *, n: int = 100, tranches: int = 4,
              today: date | None = None, symbol: str = "종목코드",
              name: str = "종목명", sector: str = "SEC_NM_KOR",
              capital: float = 0.0, close: str = "종가",
              hold_universe=None, cash: float = 0.0,
              table: str = STATE, slot_offset: int = 0,
              weighting: str = "equal", max_weight: float = 0.0,
              vol: str = "VOL") -> dict:
    """모델 순위표(picks)를 받아 이번 회차 주문을 계산하고 상태를 갱신합니다.

    picks는 qvm 오름차순(좋은 종목이 위)으로 정렬돼 있어야 합니다.

    capital > 0 이면 종목당 배정액(capital ÷ n)으로 매수 수량을 계산하고,
    1주 가격이 배정액보다 비싼 종목은 후보에서 빼고 다음 순위로 채웁니다.

    cash > 0 이면 '투입 대기 현금'으로 봅니다. 수익 재투자나 추가 입금으로
    종목당 배정액이 조금 커졌을 때, 무매매 밴드(±20%) 때문에 아무것도 사지
    않고 현금만 쌓이는 걸 막습니다. 이번 등분 몫(cash ÷ 등분수)만 쓰고,
    가장 미달인 종목부터 채웁니다 — 한 주에 몰아넣지 않습니다.

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

    price_map = None
    if close in info.columns:
        price_map = {str(k): float(v) for k, v in
                     pd.to_numeric(info[close], errors="coerce").items()
                     if pd.notna(v)}
    vol_map = None
    if vol in info.columns:
        vol_map = {str(k): float(v) for k, v in
                   pd.to_numeric(info[vol], errors="coerce").items()
                   if pd.notna(v)}

    ranked_all = picks[symbol].tolist()
    # 종목별 목표 금액 — 비중 방식이 여기서 반영됩니다
    rank_map, universe_n = None, 0
    if "전체순위" in info.columns:
        rank_map = {str(k2): float(v) for k2, v in
                    pd.to_numeric(info["전체순위"], errors="coerce").items()
                    if pd.notna(v)}
        if "전체종목수" in info.columns:
            universe_n = int(pd.to_numeric(info["전체종목수"],
                                           errors="coerce").max() or 0)
    amounts = target_amounts(ranked_all, capital=float(capital or 0.0), n=n,
                             weighting=weighting, price_map=price_map,
                             vol_map=vol_map, cap=float(max_weight or 0.0),
                             rank_map=rank_map, universe_n=universe_n)
    avg_unit = (float(capital) / max(n, 1)) if capital and capital > 0 else 0.0

    def amount_of(c):
        return amounts.get(c, avg_unit)

    ranked, too_pricey = _affordable(ranked_all, price_map, amount_of)
    target = ranked[:max(n * 2, n + sum(sizes))]
    unit = avg_unit          # 요약 표시용 평균 배정액

    state = load_state(table)
    first = state.empty

    trims: list[dict] = []          # 유지하지만 비중을 조절할 종목
    cash_orders: list[dict] = []    # 투입 대기 현금으로 채우는 주문
    if first:
        # 첫 실행 — 전액을 한 번에 넣습니다. 순위를 등분에 번갈아 나눠
        # 어느 한 등분만 상위권을 독차지하지 않게 합니다.
        rows, slot_used = [], None
        for i, code in enumerate(target[:n]):
            rows.append({"종목코드": code, "등분": i % tranches, "편입일": today,
                         "기준금액": (amount_of(code) if unit > 0
                                   else np.nan)})
        new_state = pd.DataFrame(rows)
        buys, sells, keeps = [r["종목코드"] for r in rows], [], []
    else:
        slot_used = current_slot(tranches, today, offset=slot_offset)
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
            unholdable = [c for c in keeps
                          if (price_map.get(c) or 0) > amount_of(c)]
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
                tgt = amount_of(c)
                if not tgt or tgt <= 0:
                    continue
                gap = cur - tgt
                if abs(gap) <= tgt * BAND:
                    continue
                qty = int(abs(gap) // px)
                if qty <= 0:
                    continue
                trims.append({"종목코드": c, "구분": "비중축소" if gap > 0 else "비중확대",
                              "수량": qty, "주가": px,
                              "현재기준": round(cur), "목표": round(tgt)})

        # 새로 담는 건 상위 n위에서만 — 버퍼 구간(n위~버퍼)은
        # '들고 있으면 유지, 없으면 안 산다'는 중립 구간입니다.
        # ── 투입 대기 현금 ──────────────────────────────────
        # 밴드는 '목표에서 조금 벗어난 걸 되돌리지 말자'는 규칙입니다. 그런데
        # 수익이 쌓여 배정액이 30만→33만(+10%)이 되면 전 종목이 밴드 안이라
        # 아무것도 안 사고 현금만 늘어납니다. 그래서 현금이 있을 때는 밴드를
        # 건너뛰고, 목표에 가장 모자란 종목부터 채웁니다.
        cash_orders: list[dict] = []
        budget = max(0.0, float(cash)) / max(1, tranches)
        if budget > 0 and unit > 0 and price_map:
            gaps = []
            for c in keeps:
                cur, px = base.get(c), price_map.get(c)
                if not px or px <= 0 or cur is None or not np.isfinite(cur):
                    continue
                tgt = amount_of(c)
                if tgt and tgt - cur > 0:
                    gaps.append((tgt - cur, c, px, tgt))
            gaps.sort(reverse=True)
            spent = 0.0
            for gap, c, px, tgt in gaps:
                # 목표 '정확히'까지만 채우면 1주도 못 사는 일이 생깁니다.
                # 배정액 34.2만 · 기준 33만 · 주가 3만이면 부족분이 1.2만이라
                # 1주(3만)가 안 들어갑니다. 밴드가 ±20%를 허용하니 밴드 위끝
                # 까지 채웁니다 — 덜 담긴 종목에 자연스럽게 몰리게 됩니다.
                ceiling = tgt * (1 + BAND) - cur
                room = min(max(gap, ceiling), budget - spent)
                qty = int(room // px)
                if qty <= 0:
                    continue
                spent += qty * px
                cash_orders.append({"종목코드": c, "구분": "현금투입",
                                    "수량": qty, "주가": px})
                if budget - spent < px:
                    break

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
                 "기준금액": (amount_of(c) if unit > 0
                           else base.get(c, np.nan))}
                for c in keeps]
        rows += [{"종목코드": c, "등분": slot_used, "편입일": today,
                  "기준금액": (amount_of(c) if unit > 0 else np.nan)}
                 for c in buys]
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
    store.write(table, new_state)

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
                qty = int(amount_of(c) // px)
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
    def cash_rows():
        out = []
        for t in cash_orders:
            c = t["종목코드"]
            r = info.loc[c] if c in info.index else None
            out.append({"구분": "현금투입", "종목코드": c,
                        "종목명": _label(c, name, held_name),
                        "섹터": _label(c, sector, held_sector),
                        "주가": round(t["주가"]), "수량": t["수량"],
                        "예상금액": round(t["수량"] * t["주가"]),
                        "qvm": (round(float(r["qvm"]), 4)
                                if r is not None and "qvm" in info.columns
                                and pd.notna(r["qvm"]) else None)})
        return out

    orders = pd.DataFrame(rows_for(sells, "매도") + trim_rows() + cash_rows()
                          + rows_for(buys, "매수"), columns=ORDER_COLS)

    # 이번 주 현금 수지 — 파는 돈으로 사는 돈을 댈 수 있는지.
    # 모자라면 사람이 알아야 합니다. 조용히 주문서를 내밀면 증권사 앱에서
    # 주문이 거부되고, 그게 왜인지 알 수가 없습니다.
    매도대금 = sum(float(t["수량"]) * float(t["주가"])
                for t in trims if t["구분"] == "비중축소")
    매수대금 = sum(float(r["예상금액"] or 0) for r in rows_for(buys, "매수"))
    매수대금 += sum(float(t["수량"]) * float(t["주가"])
                 for t in trims if t["구분"] == "비중확대")
    매수대금 += sum(float(t["수량"]) * float(t["주가"]) for t in cash_orders)

    return {
        "첫실행": first,
        "이번등분": slot_used,
        "등분수": tranches,
        "목표종목수": n,
        "보유종목수": len(new_state),
        "매도": len(sells), "매수": len(buys), "유지": len(keeps),
        "비중조절": len(trims),
        "현금투입": len(cash_orders),
        "현금투입금액": round(sum(t["수량"] * t["주가"] for t in cash_orders)),
        "이번주현금예산": round(max(0.0, float(cash)) / max(1, tranches)),
        "비중방식": weighting,
        "평균배정액": round(unit) if unit > 0 else None,
        "종목당배정액": round(unit) if unit > 0 else None,   # 하위호환
        "배정액범위": ((round(min(amounts.values())), round(max(amounts.values())))
                  if amounts else None),
        "가격초과제외": len(too_pricey),
        # 전량매도 종목의 금액은 보유 수량을 몰라 셀 수 없습니다.
        # 그래서 '비중축소로 확보되는 금액'만 셉니다 — 하한입니다.
        "확보금액_하한": round(매도대금),
        "필요금액": round(매수대금),
        "orders": orders,
        "holdings": new_state.sort_values(["등분", "종목코드"]).reset_index(drop=True),
    }

CAPITAL = "kor_capital"      # main 프로필 (기준일 · 평가액 · 현금 · 총자본)


def load_capital(table: str = CAPITAL) -> dict:
    """마지막으로 재동기화한 운용자금. 없으면 빈 dict."""
    df = store.read(table)
    if df.empty:
        return {}
    r = df.sort_values("기준일").iloc[-1]
    return {"기준일": r["기준일"], "평가액": float(r["평가액"]),
            "현금": float(r.get("현금", 0) or 0), "총자본": float(r["총자본"])}


def set_capital(total: float, *, cash: float = 0.0, today: date | None = None,
                table: str = CAPITAL) -> dict:
    """현금만 들고 시작하는 계좌의 운용금액을 한 번에 적어 둡니다.

    왜 resync로 안 되는가: resync는 '보유 종목 평가액'에서 총자본을 역산합니다.
    보유가 0이면 쓸 수 없습니다. 그래서 처음 현금으로 시작할 때만 이걸 씁니다.

    cash 기본값이 0인 이유: 첫 주문서는 총자본 전액을 100종목에 배분하므로
    '투입 대기 현금'이 따로 없습니다. 여기에 현금을 남겨두면 다음 주 주문서가
    이미 투자한 돈을 또 넣으려고 합니다(현금투입 주문). 첫 매수를 체결한 뒤에는
    평소대로 resync를 돌려 실제 평가액·잔여현금으로 맞추세요.
    """
    today = today or date.today()
    total = float(total)
    if total <= 0:
        raise ValueError("운용금액은 0보다 커야 합니다.")
    cash = min(max(0.0, float(cash)), total)
    store.write(table, pd.DataFrame([{
        "기준일": pd.Timestamp(today), "평가액": total - cash,
        "현금": cash, "총자본": total}]))
    log.info("운용금액 설정 — 총자본 %.0f (평가액 %.0f + 현금 %.0f)",
             total, total - cash, cash)
    return {"총자본": total, "평가액": total - cash, "현금": cash,
            "종목당배정액": total / 100}


def resync(holdings, *, cash: float = 0.0, tranches: int = 4,
           today: date | None = None, table: str = STATE,
           capital_table: str = CAPITAL) -> dict:
    """월 1회 — 실제 보유 평가액으로 기준금액과 운용자금을 다시 맞춥니다.

    왜 필요한가: 기준금액은 '마지막으로 손봤을 때 맞춰 둔 값'입니다. 주가가
    오르내리고 수익이 쌓이면 실제와 벌어지고, 그 벌어진 값으로 비중 조절
    수량을 계산하면 틀린 수량이 나옵니다. 총자본도 같습니다 — 자산이 늘면
    종목당 배정액도 커져야 하는데 고정값이면 안 따라갑니다.

    등분 배정은 그대로 둡니다. 섞으면 그 주에 포트폴리오 전체가 움직입니다.
    """
    today = today or date.today()
    got = parse_holdings(holdings)
    if got.empty:
        raise ValueError("재동기화할 보유 종목을 읽지 못했습니다.")

    state = load_state(table)
    if state.empty:
        raise ValueError("보유 상태가 없습니다. 먼저 seed를 돌리세요.")

    have = dict(zip(got["종목코드"], got["평가금액"]))
    names = dict(zip(got["종목코드"], got["종목명"]))

    st = state.copy()
    st["기준금액"] = st["종목코드"].map(have).astype(float)

    # 목록에 없는 보유 = 이미 팔린 종목. 상태에서 뺍니다.
    gone = st[st["기준금액"].isna()]["종목코드"].tolist()
    st = st[st["기준금액"].notna()]

    # 상태에 없는 보유 = 직접 사신 종목. 가장 가벼운 등분에 넣습니다.
    extra = [c for c in got["종목코드"] if c not in set(state["종목코드"])]
    if extra:
        sums = st.groupby("등분")["기준금액"].sum().to_dict()
        for j in range(max(1, int(tranches))):
            sums.setdefault(j, 0.0)
        rows = []
        for c in sorted(extra, key=lambda x: -have[x]):
            j = int(min(sums, key=lambda k: sums[k]))
            sums[j] += have[c]
            rows.append({"종목코드": c, "등분": j, "편입일": today,
                         "기준금액": have[c], "종목명": names.get(c, ""),
                         "섹터": ""})
        st = pd.concat([st, pd.DataFrame(rows)], ignore_index=True)

    st["종목명"] = st.apply(
        lambda r: names.get(r["종목코드"]) or r.get("종목명") or "", axis=1)
    st["편입일"] = pd.to_datetime(st["편입일"])
    store.write(table, st)

    평가액 = float(got["평가금액"].sum())
    현금 = max(0.0, float(cash))
    총자본 = 평가액 + 현금
    store.write(capital_table, pd.DataFrame([{
        "기준일": pd.Timestamp(today), "평가액": 평가액,
        "현금": 현금, "총자본": 총자본}]))

    by = st.groupby("등분")["기준금액"].sum()
    log.info("재동기화 — 보유 %d종목 평가액 %.0f · 현금 %.0f · 총자본 %.0f",
             len(st), 평가액, 현금, 총자본)
    return {"보유종목수": len(st), "평가액": 평가액, "현금": 현금,
            "총자본": 총자본, "종목당배정액": 총자본 / 100,
            "사라진종목": gone, "새로들어온종목": extra,
            "등분별금액": {int(k): round(v) for k, v in by.items()}}
