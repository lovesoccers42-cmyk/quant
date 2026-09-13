# -*- coding: utf-8 -*-
"""indicators.py 검증 — 교과서 정의(루프 구현) 및 statsmodels와 대조."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import factor_core as fc  # noqa: E402
import indicators as ind  # noqa: E402

rng = np.random.default_rng(42)
n = 300
close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, n))),
                  index=pd.bdate_range("2025-01-01", periods=n))


def ref_ema(s, length):
    """TA-Lib EMA: SMA 시드 + 재귀."""
    a = 2 / (length + 1)
    out = [np.nan] * len(s)
    out[length - 1] = s.iloc[:length].mean()
    for i in range(length, len(s)):
        out[i] = a * s.iloc[i] + (1 - a) * out[i - 1]
    return pd.Series(out, index=s.index)


def ref_rsi(s, length=14):
    """Wilder RSI: SMA 시드 + 1/length 재귀."""
    d = s.diff().iloc[1:]
    g = d.clip(lower=0).to_numpy()
    l = (-d.clip(upper=0)).to_numpy()
    ag = [np.nan] * len(g)
    al = [np.nan] * len(l)
    ag[length - 1] = g[:length].mean()
    al[length - 1] = l[:length].mean()
    for i in range(length, len(g)):
        ag[i] = (ag[i - 1] * (length - 1) + g[i]) / length
        al[i] = (al[i - 1] * (length - 1) + l[i]) / length
    ag, al = np.array(ag), np.array(al)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100 - 100 / (1 + ag / al)
    rsi = np.where(al == 0, 100.0, rsi)
    return pd.Series(rsi, index=d.index)


def ref_bb(s, length=20, std=2.0):
    mid = s.rolling(length).mean()
    sd = s.rolling(length).std(ddof=0)
    return mid - std * sd, mid + std * sd


fails = []


def check(name, a, b, tol=1e-9):
    a, b = a.align(b, join="inner")
    mask = a.notna() & b.notna()
    if mask.sum() == 0:
        fails.append(f"{name}: 비교할 값 없음")
        return
    diff = (a[mask] - b[mask]).abs().max()
    status = "OK " if diff < tol else "FAIL"
    if diff >= tol:
        fails.append(f"{name}: 최대 오차 {diff}")
    print(f"[{status}] {name:<28} 비교 {mask.sum():>3}개, 최대오차 {diff:.3e}")


check("EMA(12)", ind.ema(close, 12), ref_ema(close, 12))
check("EMA(26)", ind.ema(close, 26), ref_ema(close, 26))
check("RSI(14)", ind.rsi(close, 14), ref_rsi(close, 14))

lo, mid, up = ind.bbands(close, 20, 2.0)
rlo, rup = ref_bb(close, 20, 2.0)
check("BBands lower", lo, rlo)
check("BBands upper", up, rup)

m, sig = ind.macd(close)
check("MACD line", m, ref_ema(close, 12) - ref_ema(close, 26))

# ── K_ratio vs statsmodels ────────────────────────────────────
import statsmodels.api as sm  # noqa: E402

px = pd.DataFrame({f"A{i:03d}": 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
                   for i in range(25)}, index=close.index)
ret = px.pct_change().iloc[1:]
ret_cum = np.log(1 + ret).cumsum()

mine = fc.k_ratio(ret_cum)
x = np.array(range(len(ret)))
ref = {}
for c in ret_cum.columns:
    y = ret_cum[[c]]
    reg = sm.OLS(y, x).fit()
    ref[c] = float(reg.params.iloc[0] / reg.bse.iloc[0])
ref = pd.Series(ref)
check("K_ratio vs statsmodels", mine, ref, tol=1e-8)

# 신호 라벨이 실제로 생성되는지도 확인
print()
print(f"RSI 최근값 {ind.rsi(close, 14).iloc[-1]:.2f}, "
      f"MACD {m.iloc[-1]:.3f}, signal {sig.iloc[-1]:.3f}")

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전부 통과 — pandas_ta/statsmodels 없이 동일한 값을 냅니다.")
