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

# mcap_top — 안 주면 꺼져 있어야 합니다. 켜져 있으면 예전 실행과 비교가
# 안 되는데, 요약만 봐서는 모수가 좁아진 걸 눈치채기 어렵습니다.
captured.clear()
try:
    rb.main("us")
except SystemExit:
    pass
off_ok = int(captured.get("mcap_top", -1)) == 0
print(("[OK ] " if off_ok else "[FAIL] ")
      + f"mcap_top은 기본 꺼짐 ({captured.get('mcap_top')})")
if not off_ok:
    fails.append("mcap_top-default")

captured.clear()
try:
    rb.main("us", 0, "", -1.0, -1.0, -1.0, 1, 0, 0.0, "", -1.0, -1.0, -1.0,
            "", -1, -1, -1.0, "", "", 500)
except SystemExit:
    pass
on_ok = int(captured.get("mcap_top", 0)) == 500
print(("[OK ] " if on_ok else "[FAIL] ")
      + f"mcap_top=500이 그대로 전달됨 ({captured.get('mcap_top')})")
if not on_ok:
    fails.append("mcap_top-pass")

# 리포트 파일명 — GitHub 릴리스는 에셋 이름에서 한글을 떼어냅니다. 영문 토큰이
# 없으면 서로 다른 리포트가 "1006_100.xlsx" 하나로 합쳐져 덮어써집니다
# (10-06에 주간 주문서가 백테스트 리포트에 덮여 실제로 사라졌습니다).
def ascii_only(name: str) -> str:
    return "".join(c for c in name if ord(c) < 128).strip()


day = "20261008"
names = {
    "모델-main": config.report_filename("한국", day, token="model-kr-main"),
    "모델-alt": config.report_filename("한국-배우자", day, token="model-kr-alt"),
    "모델-미국": config.report_filename("미국", day, token="model-us"),
    "주간-main": f"x 한국 주간주문 weekly-main-{day[4:]}_100.xlsx",
    "주간-alt": f"x 한국 주간주문 배우자 weekly-alt-{day[4:]}_100.xlsx",
    "백테스트-kr": f"x 백테스트 한국 backtest-kr-{day[4:]}_100.xlsx",
}
stripped = {k: ascii_only(v) for k, v in names.items()}
uniq = len(set(stripped.values())) == len(stripped)
print(("[OK ] " if uniq else "[FAIL] ")
      + f"한글을 떼어내도 리포트 이름이 서로 다름 ({len(set(stripped.values()))}"
        f"/{len(stripped)}종)")
if not uniq:
    fails.append("filename")
    for k, v in stripped.items():
        print(f"    {k} -> {v}")

# ── 시장별 확정 설정이 정말 시장별인가 ─────────────────────────────────
# 2026-10-07 실제 사고: 워크플로우가 모든 칸을 한국 기본값으로 채워 보내
# 미국 런에 유동성 하한 5억이 '5억 달러'로 들어갔습니다. 통과 종목이 47개로
# 줄어 지수 대비 연 -6.7%p가 나왔고, 그 결과를 전략 평가로 읽을 뻔했습니다.
# 워크플로우는 이제 칸을 비워 보내므로, 빈 칸이 시장별 확정값으로 풀리는지
# 여기서 못 박습니다.
print("\n[시장별 확정 설정]")
BLANK = [0, "", -1.0, -1.0, -1.0, -1, -1, -1.0, "", -1.0, -1.0, -1.0,
         "", -1, -1, -1.0, "", ""]        # 워크플로우가 빈 칸을 보낸 상태

for market, ck in (("kr", config.CONFIRMED_KR),
                   ("us", getattr(config, "CONFIRMED_US", {}))):
    if not ck:
        continue
    captured.clear()
    try:
        rb.main(market, *BLANK)
    except SystemExit:
        pass
    for key in ("min_turnover", "cost_bps", "top_n", "tranches", "weighting",
                "rebal_band", "rank_buffer", "sector_neutral", "max_sector_pct"):
        want = ck.get(key)
        got = captured.get(key)
        ok = (want is None) or (got == want)
        print(("[OK ] " if ok else "[FAIL] ")
              + f"{market} {key} = {got!r} (확정 {want!r})")
        if not ok:
            fails.append(f"{market}:{key}")

# 두 시장의 유동성·비용이 실제로 달라야 합니다 — 같으면 한쪽이 덮인 것입니다
vals = {}
for market in ("kr", "us"):
    captured.clear()
    try:
        rb.main(market, *BLANK)
    except SystemExit:
        pass
    vals[market] = (captured.get("min_turnover"), captured.get("cost_bps"))
diff = vals["kr"] != vals["us"]
print(("[OK ] " if diff else "[FAIL] ")
      + f"한국 {vals['kr']} vs 미국 {vals['us']} — 달라야 정상")
if not diff:
    fails.append("market_mix")

# 가드: 미국에 한국 유동성을 넣으면 멈춰야 합니다
try:
    rb.main("us", 0, "", -1.0, -1.0, 500_000_000.0, -1, -1, -1.0, "",
            -1.0, -1.0, -1.0, "", -1, -1, -1.0, "", "")
    blocked = False
except ValueError:
    blocked = True
except SystemExit:
    blocked = False
print(("[OK ] " if blocked else "[FAIL] ")
      + "미국에 한국 유동성(5억)을 넣으면 멈춤")
if not blocked:
    fails.append("guard")

print("\n전체 통과 — 인자 없이 돌려도 확정 설정이 들어갑니다." if not fails
      else "\n실패: " + ", ".join(fails))
sys.exit(1 if fails else 0)
