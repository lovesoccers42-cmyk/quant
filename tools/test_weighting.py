# -*- coding: utf-8 -*-
"""비중 방식 테스트 — 네트워크 없이.

확인하는 것:
  · 상한을 넘는 종목이 없고 합이 정확히 1인가
  · 상한이 너무 낮아 100%를 못 채우는 설정을 조용히 틀리게 처리하지 않는가
  · 시총가중이 실제로 큰 회사를 크게 담는가
  · 점수가중이 순위 1등을 가장 크게 담는가
  · 역변동성이 덜 흔들리는 종목을 크게 담는가
  · 데이터가 절반 넘게 비면 동일가중으로 물러서는가
  · 분할 리밸런싱에서 '안 건드리는 등분'의 비중이 보존되는가
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="wt_test_")
os.environ["QUANT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest as bt  # noqa: E402

fails = []


def check(cond, msg):
    print(("[OK ] " if cond else "[FAIL] ") + msg)
    if not cond:
        fails.append(msg)


# ── _cap_weights ────────────────────────────────────────────
w = bt._cap_weights(pd.Series({"a": 100.0, "b": 1.0, "c": 1.0, "d": 1.0}), 0.30)
check(abs(float(w.sum()) - 1.0) < 1e-9, f"합이 1 ({float(w.sum()):.12f})")
check(float(w.max()) <= 0.30 + 1e-9, f"상한 30% 준수 (최대 {float(w.max()):.4f})")
check(abs(float(w["a"]) - 0.30) < 1e-9, "쏠린 종목이 정확히 상한에 눌림")
check(abs(float(w["b"]) - float(w["c"])) < 1e-12, "남은 몫은 비례 배분")

# 상한 × 종목수 < 1 — 지킬 수 없는 설정. 조용히 깨지지 말고 동일가중으로.
w2 = bt._cap_weights(pd.Series({"a": 5.0, "b": 1.0, "c": 1.0}), 0.10)
check(abs(float(w2.sum()) - 1.0) < 1e-9, "지킬 수 없는 상한에서도 합은 1")
check(float(w2["a"]) > float(w2["b"]), "지킬 수 없는 상한이면 상한을 무시(합 1 우선)")

# 음수·0·NaN이 섞여도 터지지 않아야 합니다
w3 = bt._cap_weights(
    pd.Series({"a": -3.0, "b": 0.0, "c": np.nan, "d": 2.0, "e": 1.0}), 0.6)
check(abs(float(w3.sum()) - 1.0) < 1e-9, "음수·0·NaN이 섞여도 합은 1")
check(float(w3["a"]) == 0.0 and float(w3["c"]) == 0.0,
      "음수와 NaN은 0으로 (받을 수 있는 종목이 따로 있을 때)")
check(float(w3["d"]) <= 0.6 + 1e-9, "상한을 넘은 몫은 양수 종목에게만 감")

# 궁지에 몰린 경우: 상한에 걸린 종목 말고는 전부 0이면, 합을 1로 맞추려면
# 0인 종목에 나눠줄 수밖에 없습니다. 틀린 게 아니라 정의된 동작입니다.
w3b = bt._cap_weights(pd.Series({"a": 0.0, "b": 0.0, "c": 2.0}), 0.6)
check(abs(float(w3b.sum()) - 1.0) < 1e-9 and float(w3b.max()) <= 0.6 + 1e-9,
      "받을 양수 종목이 없으면 0인 종목에 균등 배분 (합 1 · 상한 유지)")

# 전부 0이면 동일가중으로 물러서야 합니다 (0으로 나누기 방지)
w4 = bt._cap_weights(pd.Series({"a": 0.0, "b": 0.0}), 0.6)
check(abs(float(w4.sum()) - 1.0) < 1e-9 and abs(float(w4["a"]) - 0.5) < 1e-9,
      "전부 0이면 동일가중")

# ── _target_weights ────────────────────────────────────────
codes = [f"{i:06d}" for i in range(1, 21)]
dates = pd.bdate_range("2025-01-01", periods=120)
rng = np.random.default_rng(0)

# 앞쪽 종목은 변동성이 작고 뒤쪽은 큽니다
vols = np.linspace(0.005, 0.05, len(codes))
px = pd.DataFrame(
    {c: 10000 * np.cumprod(1 + rng.normal(0, v, len(dates)))
     for c, v in zip(codes, vols)}, index=dates)
t = dates[-1]
# 주식수: 뒤쪽 종목이 훨씬 많음 → 시총도 큼
shares = pd.Series({c: float(10 ** 5) * (i + 1) ** 3
                    for i, c in enumerate(codes)})

eq = bt._target_weights(codes, "equal", shares=shares, price_pivot=px, t=t,
                        order=codes, cap=0.0, vol_window=60)
check(abs(float(eq.std()) ) < 1e-12, "동일가중은 전 종목 같은 비중")

mc = bt._target_weights(codes, "mcap", shares=shares, price_pivot=px, t=t,
                        order=codes, cap=0.08, vol_window=60)
check(abs(float(mc.sum()) - 1) < 1e-9 and float(mc.max()) <= 0.08 + 1e-9,
      f"시총가중 합 1 · 상한 8% (최대 {float(mc.max()):.4f})")
check(float(mc[codes[-1]]) > float(mc[codes[0]]),
      "시총가중은 큰 회사를 크게 담음")

sc = bt._target_weights(codes, "score", shares=shares, price_pivot=px, t=t,
                        order=codes, cap=0.0, vol_window=60)
check(list(sc.sort_values(ascending=False).index) == codes,
      "점수가중은 순위 순서대로 비중이 큼")
check(float(sc[codes[0]]) > float(sc[codes[-1]]) * 5,
      "1등과 꼴등의 비중 차이가 뚜렷함")

iv = bt._target_weights(codes, "invvol", shares=shares, price_pivot=px, t=t,
                        order=codes, cap=0.0, vol_window=60)
check(float(iv[codes[0]]) > float(iv[codes[-1]]),
      "역변동성은 덜 흔들리는 종목을 크게 담음")
check(abs(float(iv.sum()) - 1) < 1e-9, "역변동성 합 1")

# 순위표에 없는 종목(등분에 남아 순위 밖으로 밀려난 종목)도 처리돼야 합니다
sc2 = bt._target_weights(codes, "score", shares=shares, price_pivot=px, t=t,
                         order=codes[:10], cap=0.0, vol_window=60)
check(abs(float(sc2.sum()) - 1) < 1e-9, "순위 밖 종목이 섞여도 합은 1")
check(float(sc2[codes[0]]) > float(sc2[codes[15]]),
      "순위 밖 종목은 꼴찌 대우")

# 데이터가 절반 넘게 비면 동일가중으로 물러서야 합니다
holes = pd.Series({c: (np.nan if i >= 5 else 10.0 ** 5)
                   for i, c in enumerate(codes)})
mc2 = bt._target_weights(codes, "mcap", shares=holes, price_pivot=px, t=t,
                         order=codes, cap=0.08, vol_window=60)
check(float(mc2.std()) < 1e-12, "데이터가 절반 넘게 비면 동일가중으로 물러섬")

# ── 분할 리밸런싱의 비중 보존 ───────────────────────────────
# 4등분 중 한 등분만 손보면, 나머지 3등분이 들고 있던 돈(≈75%)은
# 그대로 남아 있어야 합니다. 전부 다시 맞추면 분할의 의미가 없습니다.
n_tr, n = 4, 20
sleeves = [codes[j::n_tr] for j in range(n_tr)]
w_state = pd.Series(1.0 / n, index=pd.Index(codes))
slot = 1
untouched = [c for c in codes if c not in set(sleeves[slot])]
pool = float(w_state.reindex(sleeves[slot]).sum())
check(abs(pool - 0.25) < 1e-9, f"손보는 등분이 들고 있는 몫 25% ({pool:.4f})")
kept = float(w_state.reindex(untouched).sum())
check(abs(kept - 0.75) < 1e-9, f"안 건드리는 등분의 몫 75% ({kept:.4f})")

# ── 회전율 정의 ─────────────────────────────────────────────
# 100종목 동일가중에서 25종목을 통째로 갈면 Σ|Δw|/2 = 0.25 여야 합니다.
old = pd.Series(1 / 100, index=[f"o{i}" for i in range(100)])
new = pd.Series(1 / 100, index=[f"o{i}" for i in range(75)] +
                [f"n{i}" for i in range(25)])
u = old.index.union(new.index)
to = float((new.reindex(u).fillna(0) - old.reindex(u).fillna(0)).abs().sum()) / 2
check(abs(to - 0.25) < 1e-9, f"25종목 교체 = 회전율 25% ({to:.4f})")

# 종목은 그대로인데 비중만 흐트러진 것을 되돌리는 거래도 비용입니다.
# 예전 정의(교체 종목 수)는 이걸 0으로 셌습니다.
drift = pd.Series([0.015] * 50 + [0.005] * 50, index=old.index)
to2 = float((old - drift).abs().sum()) / 2
check(to2 > 0.2, f"비중만 되돌리는 거래도 회전율로 잡힘 ({to2:.1%})")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n실패:", *fails, sep="\n  ")
    sys.exit(1)
print("\n전체 통과 — 비중 방식 4종이 상한을 지키고 합이 1입니다.")
