# -*- coding: utf-8 -*-
"""생존편향을 **직접** 재봅니다 — 네트워크 없이, 쌓아 둔 종목표만으로.

    python tools/probe_survivorship.py            # 기본 (상위 100종목)
    python tools/probe_survivorship.py 50         # 상위 50종목으로

무엇을 재는가
-------------
백테스트는 `_load`가 종목표의 **최신 기준일 한 줄**만 읽습니다. 즉 '지금
상장돼 있는 회사'로 과거를 계산합니다. 그 사이 상장폐지된 회사는 통째로
빠지고, 그 회사들의 마지막 -80%도 같이 빠집니다. 성과는 실제보다 좋게
나옵니다.

예전 버전은 pykrx로 '그때 상장 종목 수'만 셌습니다. 그건 유니버스 전체의
감소율일 뿐, **우리 전략이 그 종목들을 샀을지**는 말해주지 않습니다. 유동성
5억 필터를 통과하는 1,500종목 안에서는 폐지가 훨씬 드물고, 반대로 가치
팩터는 '싸 보이는' 부실주를 끌어올 수 있습니다. 둘 중 어느 쪽이 큰지는
세어 봐야 압니다.

kor_ticker에는 2023-09부터 기준일별 스냅샷이 쌓여 있습니다. 그래서 이제
이렇게 잴 수 있습니다:

  A. 지금 종목표로 그 시점 점수를 매긴다  (= 현재 백테스트가 하는 일)
  B. 그 시점 종목표로 점수를 매긴다       (= 그때 실제로 살 수 있던 종목)

둘의 상위 N종목과 그 뒤 12개월 수익률을 비교하면, 생존편향이 성과를 몇 %p
부풀리는지가 바로 나옵니다.

폐지 종목의 처리 — 두 가지로 모두 계산합니다
  · 낙관: 마지막 체결가에 전량 매도해 현금 보유 (정리매매에서 다 건졌다)
  · 비관: 마지막 체결가에서 -100% (한 푼도 못 건졌다)
실제는 둘 사이입니다. 두 숫자 사이를 편향의 범위로 보시면 됩니다.

한계: 2023-09 이전 스냅샷이 없어 그 구간은 못 잽니다. 2023~2026 측정치를
그 이전에도 비슷하다고 가정하는 셈인데, 2017~2020은 폐지가 더 많았던
시기라 실제 편향은 여기 숫자보다 클 가능성이 높습니다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest as bt  # noqa: E402
import config  # noqa: E402
import store  # noqa: E402

HOLD_DAYS = 365          # 매수 후 보유 기간
MIN_HISTORY_DAYS = 400   # 모멘텀에 1년 + 여유


def _snapshots(ticker: pd.DataFrame, spec) -> pd.Series:
    d = pd.to_datetime(ticker[spec.t_date])
    return pd.Series(sorted(d[d > "2000-01-01"].unique()))


def _pick_dates(snaps: pd.Series, px_first, px_last, n: int = 8) -> list:
    """모멘텀 1년과 보유 1년이 모두 확보되는 스냅샷을 고르게 n개 고릅니다."""
    lo = px_first + pd.Timedelta(days=MIN_HISTORY_DAYS)
    hi = px_last - pd.Timedelta(days=HOLD_DAYS)
    ok = [d for d in snaps if lo <= d <= hi]
    if len(ok) <= n:
        return ok
    idx = np.linspace(0, len(ok) - 1, n).round().astype(int)
    return [ok[i] for i in sorted(set(idx))]


def _forward(px: pd.DataFrame, codes, t, horizon_end, dead: set):
    """t → horizon_end 동일가중 수익률. (낙관, 비관) 두 가지."""
    opt, pes = [], []
    for c in codes:
        if c not in px.columns:
            continue
        s = px[c].loc[:horizon_end].dropna()
        s0 = s.loc[:t]
        if s0.empty or len(s) == 0:
            continue
        p0 = float(s0.iloc[-1])
        if p0 <= 0:
            continue
        p1 = float(s.iloc[-1])
        r = p1 / p0 - 1
        gone = c in dead and s.index[-1] < horizon_end - pd.Timedelta(days=10)
        opt.append(r)                      # 마지막 체결가에 팔았다
        pes.append(-1.0 if gone else r)    # 한 푼도 못 건졌다
    if not opt:
        return np.nan, np.nan, 0
    return float(np.mean(opt)), float(np.mean(pes)), len(opt)


def main(top_n: int = None) -> int:
    top_n = int(top_n or config.N_PORTFOLIO)
    spec = bt.SPECS["kr"]
    conf = config.CONFIRMED_KR

    price, fs, ticker_now, sector = bt._load(spec)
    price[spec.p_date] = pd.to_datetime(price[spec.p_date])
    fs[spec.f_date] = pd.to_datetime(fs[spec.f_date])

    # 종목표 전체(스냅샷 포함)를 따로 읽습니다 — _load는 최신 한 줄만 줍니다.
    allt = store.read(spec.ticker)
    allt[spec.t_date] = pd.to_datetime(allt[spec.t_date])
    allt[spec.t_sym] = allt[spec.t_sym].astype(str).str.zfill(6)
    if spec.common_only and "종목구분" in allt.columns:
        allt = allt[allt["종목구분"] == "보통주"]

    snaps = _snapshots(allt, spec)
    if len(snaps) < 2:
        print("종목표에 과거 스냅샷이 없습니다 — 아직 이 방법으로는 못 잽니다.")
        return 2

    px_all = price.pivot_table(index=spec.p_date, columns=spec.p_sym,
                               values=spec.p_close, aggfunc="last").sort_index()
    px_all.columns = [str(c).zfill(6) for c in px_all.columns]
    tv = price.assign(_v=pd.to_numeric(price[spec.p_close], errors="coerce")
                      * pd.to_numeric(price[spec.p_vol], errors="coerce"))
    tvp = tv.pivot_table(index=spec.p_date, columns=spec.p_sym,
                         values="_v", aggfunc="last").sort_index()
    tvp.columns = [str(c).zfill(6) for c in tvp.columns]

    now_set = set(allt[allt[spec.t_date] == allt[spec.t_date].max()][spec.t_sym])
    dates = _pick_dates(snaps, px_all.index.min(), px_all.index.max())
    if not dates:
        print("주가 기간이 짧아 (모멘텀 1년 + 보유 1년) 잴 수 있는 시점이 없습니다.")
        return 2

    print(f"종목표 스냅샷 {len(snaps)}개 "
          f"({snaps.iloc[0]:%Y-%m-%d} ~ {snaps.iloc[-1]:%Y-%m-%d})")
    print(f"주가 {px_all.index.min():%Y-%m-%d} ~ {px_all.index.max():%Y-%m-%d}")
    print(f"상위 {top_n}종목 · 보유 {HOLD_DAYS}일 · "
          f"유동성 {conf['min_turnover']:,.0f}원\n")

    rows = []
    cache_a, cache_b = {}, {}
    for t in dates:
        t = pd.Timestamp(t)
        end = t + pd.Timedelta(days=HOLD_DAYS)
        snap = allt[allt[spec.t_date] == t]
        if snap.empty:
            continue
        dead = set(snap[spec.t_sym]) - now_set

        win = tvp.loc[:t].tail(config.LIQUIDITY_WINDOW).mean()
        eligible = set(win[win >= conf["min_turnover"]].index)

        out = {}
        for tag, tk, cache in (("A", ticker_now, cache_a), ("B", snap, cache_b)):
            tk = tk.copy()
            tk[spec.t_sym] = tk[spec.t_sym].astype(str).str.zfill(6)
            shares = bt._shares(tk, spec, px_all)
            scored = bt._score_at(t, px_all, fs, tk, sector, shares, spec,
                                  lag_days=90, ttm_cache=cache,
                                  neutral=bt.fc.neutral_spec(
                                      conf["sector_neutral"]))
            if scored is None or scored.empty:
                out[tag] = None
                continue
            s = scored[scored["symbol"].astype(str).isin(eligible)]
            codes = s.sort_values("qvm")["symbol"].astype(str).head(top_n).tolist()
            o, p, n = _forward(px_all, codes, t, end, dead)
            out[tag] = {"codes": codes, "낙관": o, "비관": p, "n": n,
                        "폐지": len([c for c in codes if c in dead]),
                        "풀": len(s)}
        if not out.get("A") or not out.get("B"):
            continue
        a, b = out["A"], out["B"]
        # 재무 데이터가 얕아 점수풀이 top_n도 안 되는 시점은 버립니다 —
        # 그런 시점의 '상위 100종목'은 사실 전 종목이라 비교가 성립하지 않습니다.
        if min(a["풀"], b["풀"]) < top_n * 2:
            print(f"  {t:%Y-%m-%d} 건너뜀 — 점수풀 {a['풀']}/{b['풀']}종목 "
                  f"(상위 {top_n} 비교가 성립하지 않음)")
            continue
        rows.append({
            "시점": f"{t:%Y-%m-%d}",
            "지금 없는 종목": len(dead),
            "점수풀 A/B": f"{a['풀']}/{b['풀']}",
            "상위에 낀 폐지종목": b["폐지"],
            "A 생존자만": a["낙관"], "B 낙관": b["낙관"], "B 비관": b["비관"],
            "폐지비용": b["낙관"] - b["비관"],
            "A-B(낙관)": a["낙관"] - b["낙관"],
            "겹침": len(set(a["codes"]) & set(b["codes"])) / max(len(a["codes"]), 1),
        })

    if not rows:
        print("잴 수 있는 시점이 없었습니다.")
        return 2

    df = pd.DataFrame(rows)
    show = df.copy()
    for c in ("A 생존자만", "B 낙관", "B 비관", "폐지비용", "A-B(낙관)"):
        show[c] = (show[c] * 100).map(lambda v: f"{v:+.1f}%")
    show["겹침"] = (show["겹침"] * 100).map(lambda v: f"{v:.0f}%")
    print(show.to_string(index=False))

    print("\n  읽는 법")
    print("   · 폐지비용 = B 낙관 - B 비관. 같은 종목 목록을 놓고 폐지 종목의")
    print("     회수액만 바꾼 값이라 종목 구성 차이가 섞이지 않습니다.")
    print("     생존편향의 순수한 상한입니다 (실제 회수액은 0과 마지막 체결가 사이).")
    print("   · A-B = 종목 구성까지 바뀐 차이. 겹침이 90% 안팎이라 '다른 종목을")
    print("     들었다'는 잡음이 같이 들어갑니다 - 아래 표준편차와 비교하세요.")

    cost = df["폐지비용"].mean() * 100
    ab = df["A-B(낙관)"] * 100
    print(f"\n  연 폐지비용(상한): 평균 {cost:+.2f}%p "
          f"(최대 {df['폐지비용'].max()*100:+.2f}%p)")
    print(f"  A-B: 평균 {ab.mean():+.2f}%p · 표준편차 {ab.std(ddof=1):.2f}%p "
          f"(시점 {len(df)}개)")
    print(f"  상위 {top_n}종목에 폐지 예정 종목이 섞인 횟수: "
          f"{df['상위에 낀 폐지종목'].sum()}건 / {len(df)}개 시점")

    ref = 6.68   # 유동성통과 대비 연 초과수익 (확정 설정 실측치)
    print(f"\n  판정 (유동성통과 대비 연 초과수익 +{ref}%p와 비교):")
    if cost < ref * 0.2:
        print(f"   · 최악으로 쳐도 초과수익의 {cost/ref*100:.0f}%입니다. 작습니다.")
        print("   · 유동성 5억 필터가 부실주를 대부분 먼저 걸러주기 때문입니다.")
        print("   · 기간 연장(백필)을 진행해도 됩니다.")
    elif cost < ref * 0.6:
        print(f"   · 최악으로 치면 초과수익의 {cost/ref*100:.0f}%를 먹습니다.")
        print("   · 백필 전에 시점별 유니버스를 쓰도록 _load를 고치세요 "
              "(종목표 스냅샷은 이미 쌓이고 있습니다).")
    else:
        print(f"   · 최악으로 치면 초과수익의 {cost/ref*100:.0f}%입니다. 측정된")
        print("     우위의 상당 부분이 '망한 회사가 빠져서' 생긴 것일 수 있습니다.")
        print("     이 상태로 기간만 늘리는 건 틀린 숫자를 더 정밀하게 재는 일입니다.")
    if abs(ab.mean()) < ab.std(ddof=1):
        print("   · A-B의 평균이 표준편차보다 작습니다 - 종목 구성까지 포함한")
        print("     차이는 잡음에 묻혀 있습니다. 폐지비용 쪽을 보세요.")

    print("\n  주의: 2023-09 이전 스냅샷이 없어 그 구간은 못 쟀습니다. "
          "2017~2020은\n  폐지가 더 잦았으니 실제 편향은 위 숫자보다 클 수 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
