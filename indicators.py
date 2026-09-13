# -*- coding: utf-8 -*-
"""기술적 지표 — MACD / RSI / 볼린저밴드.

기존에는 pandas_ta를 썼지만 numpy 2.x와 호환되지 않아 클라우드에서
설치가 깨집니다. 동일한 정의를 pandas만으로 직접 구현했습니다.

정의는 pandas_ta 기본값과 동일:
  EMA   : adjust=False 지수이동평균
  MACD  : EMA(12) - EMA(26), 시그널 EMA(9)
  RSI   : Wilder 방식(RMA) 14기간
  BBands: SMA(20) ± 2 * 표준편차(ddof=0)
"""
import numpy as np
import pandas as pd


def ema(s: pd.Series, length: int) -> pd.Series:
    """지수이동평균 — TA-Lib/pandas_ta 방식(첫 length개 SMA를 시드로 사용)."""
    s = s.astype("float64").dropna()
    if len(s) < length:
        return pd.Series(np.nan, index=s.index, dtype="float64")
    seeded = s.copy()
    seeded.iloc[:length - 1] = np.nan
    seeded.iloc[length - 1] = s.iloc[:length].mean()
    return seeded.ewm(span=length, adjust=False, ignore_na=True).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """(macd, signal) 튜플 반환."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line.dropna(), signal).reindex(close.index)
    return macd_line, signal_line


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder RSI. pandas_ta.rsi와 동일한 RMA 평활."""
    close = close.astype("float64").dropna()
    if len(close) < length + 1:
        return pd.Series(np.nan, index=close.index)

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder 평활: 첫 length개 평균을 시드로 쓰고 alpha=1/length 로 재귀 (TA-Lib 방식)
    def _wilder(x: pd.Series) -> pd.Series:
        x = x.iloc[1:]                      # diff의 첫 NaN 제거
        seeded = x.copy()
        seeded.iloc[:length - 1] = np.nan
        seeded.iloc[length - 1] = x.iloc[:length].mean()
        return seeded.ewm(alpha=1 / length, adjust=False, ignore_na=True).mean()

    avg_gain = _wilder(gain)
    avg_loss = _wilder(loss)

    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    # 손실이 0이면 RSI = 100
    out = out.where(avg_loss != 0, 100.0)
    return out


def bbands(close: pd.Series, length: int = 20, std: float = 2.0):
    """(lower, mid, upper) 반환."""
    close = close.astype("float64").dropna()
    mid = close.rolling(length, min_periods=length).mean()
    sd = close.rolling(length, min_periods=length).std(ddof=0)
    return mid - std * sd, mid, mid + std * sd


# K_ratio(모멘텀 팩터)는 한·미 공통 계산이라 factor_core.py에 있습니다.
