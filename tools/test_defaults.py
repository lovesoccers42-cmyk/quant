"""확정 기본값 테스트 — 네트워크 없이.

확인하는 것:
  · 인자를 하나도 안 줘도 확정 설정(CONFIRMED_KR)이 그대로 들어가는가
  · 인자를 주면 확정값을 덮어쓰는가
  · 미국장은 확정값에 영향받지 않는가

왜 필요한가: 확정 설정을 코드에 적어두고 워크플로우에서 손으로 입력하게 두면,
한 칸 잘못 넣은 채로 돌아갑니다 — 실제로 max_weight 칸에 0.25를 넣어 시총가중
런 하나가 통째로 무효가 됐습니다.
"""
import os, sys, tempfile
os.environ["QUANT_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, run_backtest as rb

captured = {}
import backtest as bt
_orig = bt.run
def fake(**kw):
    captured.update(kw)
    raise SystemExit(0)      # 실제 실행은 하지 않습니다
bt.run = fake
try:
    rb.main("kr")            # 인자 전혀 없이
except SystemExit:
    pass
CK = config.CONFIRMED_KR
fails = []
for k, want in CK.items():
    if k == "qvm_weights":
        continue
    got = captured.get(k)
    if k == "use_lowvol":
        ok = bool(got) == bool(want)
    elif isinstance(want, float):
        ok = got is not None and abs(float(got) - want) < 1e-9
    else:
        ok = got == want
    print(("[OK ] " if ok else "[FAIL] ") + f"{k}: {got!r} (확정 {want!r})")
    if not ok:
        fails.append(k)
print(f"[{'OK ' if captured.get('top_n') == 100 else 'FAIL'}] top_n: {captured.get('top_n')}")
# 인자를 주면 그게 이겨야 합니다
captured.clear()
try:
    rb.main("kr", 100, "ME", 25.0, 0.25, -1.0, 1, 0, 0.0, "equal")
except SystemExit:
    pass
over = (captured.get("rebalance") == "ME" and captured.get("weighting") == "equal"
        and abs(captured.get("max_sector_pct") - 0.25) < 1e-9)
print(("[OK ] " if over else "[FAIL] ") + f"인자가 확정값을 덮어씀 "
      f"(rebalance {captured.get('rebalance')}, weighting {captured.get('weighting')}, "
      f"섹터상한 {captured.get('max_sector_pct')})")
if not over:
    fails.append("override")
# 미국장은 영향받지 않아야 합니다
captured.clear()
try:
    rb.main("us")
except SystemExit:
    pass
CU = config.CONFIRMED_US
us_ok = all(
    (bool(captured.get(k)) == bool(v)) if k == "use_lowvol"
    else (abs(float(captured.get(k)) - v) < 1e-9 if isinstance(v, float)
          else captured.get(k) == v)
    for k, v in CU.items() if k != "qvm_weights")
print(("[OK ] " if us_ok else "[FAIL] ") + "미국장도 확정값(CONFIRMED_US)이 들어감")
print(f"       비용 {captured.get('cost_bps')}bp · 유동성 {captured.get('min_turnover'):,.0f}"
      f" · 구조는 한국과 동일(비중 {captured.get('weighting')}, 등분 {captured.get('tranches')})")
if not us_ok:
    fails.append("us")

# 시장을 바꿀 때 가장 쉽게 틀리는 값 — 코드가 막아야 합니다
for mkt, turn, why in (("us", 500_000_000, "미국에 한국 원화 기준값"),
                       ("kr", 5_000_000, "한국에 달러 기준값")):
    try:
        rb.main(mkt, 0, "", -1.0, -1.0, turn)
        print(f"[FAIL] {why}을 그냥 받아들임")
        fails.append(f"{mkt}-turnover")
    except SystemExit:
        print(f"[FAIL] {why}을 그냥 받아들임 (백테스트까지 진행)")
        fails.append(f"{mkt}-turnover")
    except ValueError as e:
        ok = "그대로 넣은 것 같습니다" in str(e) or "넣은 것 같습니다" in str(e)
        print(("[OK ] " if ok else "[FAIL] ") + f"{why}은 막힘")
        if not ok:
            fails.append(f"{mkt}-turnover")

# 빈 문자열은 '안 줬음'으로 처리돼야 합니다 (워크플로우가 빈 칸을 보냄)
captured.clear()
try:
    rb.main("us", 0, "", -1.0, -1.0, -1.0, 1, 0, 0.0, "", -1.0, -1.0, -1.0, "")
except SystemExit:
    pass
blank_ok = (captured.get("weighting") == config.CONFIRMED_US["weighting"]
            and abs(captured.get("cost_bps") - config.CONFIRMED_US["cost_bps"]) < 1e-9)
print(("[OK ] " if blank_ok else "[FAIL] ")
      + f"빈 칸은 확정값으로 채워짐 (비중 {captured.get('weighting')}, "
        f"비용 {captured.get('cost_bps')}bp)")
if not blank_ok:
    fails.append("blank")

print("\n전체 통과 — 인자 없이 돌려도 확정 설정이 들어갑니다." if not fails
      else "\n실패: " + ", ".join(fails))
sys.exit(1 if fails else 0)
