# -*- coding: utf-8 -*-
"""전역 설정. 모든 값은 환경변수(GitHub Secrets / Streamlit secrets)로 덮어씁니다.

로컬 MySQL은 더 이상 쓰지 않습니다. 데이터는 data/*.parquet 파일에 저장되고
DuckDB로 조회합니다(store.py).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ── 경로 ─────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("QUANT_DATA_DIR", BASE_DIR / "data"))   # parquet 데이터 저장소
OUTPUT_DIR = Path(os.getenv("QUANT_OUTPUT_DIR", BASE_DIR / "output"))  # 모델 결과
LOG_DIR = Path(os.getenv("QUANT_LOG_DIR", BASE_DIR / "logs"))

for _d in (DATA_DIR, OUTPUT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 수집 설정 ────────────────────────────────────────────────
# 클라우드에서는 병렬 수집으로 시간을 줄입니다. 차단당하면 워커 수를 낮추세요.
PRICE_WORKERS = int(os.getenv("QUANT_PRICE_WORKERS", "6"))
FS_WORKERS = int(os.getenv("QUANT_FS_WORKERS", "3"))
PRICE_SLEEP = float(os.getenv("QUANT_PRICE_SLEEP", "0.15"))  # 워커별 요청 간격(초)
FS_SLEEP = float(os.getenv("QUANT_FS_SLEEP", "0.8"))

DAILY_PRICE_DAYS = int(os.getenv("QUANT_DAILY_PRICE_DAYS", "10"))
MONTHLY_PRICE_YEARS = int(os.getenv("QUANT_MONTHLY_PRICE_YEARS", "2"))
# 팩터 모델 자체는 최근 1년 주가면 되지만, 백테스트 기간이 곧 통계적 신뢰도라
# (t = IR x √기간) 주가는 길게 보관합니다. DART로 재무를 2015년까지 끌어와도
# 주가가 2년치면 백테스트는 여전히 1년밖에 못 돕니다.
# 12년치 kor_price는 약 85MB로 릴리스 용량(2GB)에 한참 못 미칩니다.
PRICE_KEEP_YEARS = int(os.getenv("QUANT_PRICE_KEEP_YEARS", "12"))

HTTP_TIMEOUT = int(os.getenv("QUANT_HTTP_TIMEOUT", "20"))
HTTP_RETRIES = int(os.getenv("QUANT_HTTP_RETRIES", "3"))

# ── 미국장 수집 설정 ─────────────────────────────────────────
# yfinance·yahooquery는 심볼을 묶어 받을 수 있습니다. 묶음이 클수록 빠르지만
# Yahoo가 데이터센터 IP를 제한하면 통째로 실패하므로 적당히 잡습니다.
YF_CHUNK = int(os.getenv("QUANT_YF_CHUNK", "100"))    # 주가 한 번에 받을 심볼 수
YF_SLEEP = float(os.getenv("QUANT_YF_SLEEP", "1.0"))  # 묶음 사이 간격(초)
FS_CHUNK = int(os.getenv("QUANT_FS_CHUNK", "50"))     # yahooquery 묶음 크기
US_FS_WORKERS = int(os.getenv("QUANT_US_FS_WORKERS", "8"))  # 재무제표 동시 조회 수
US_DAILY_PRICE_DAYS = int(os.getenv("QUANT_US_DAILY_PRICE_DAYS", "10"))
US_MONTHLY_PRICE_YEARS = int(os.getenv("QUANT_US_MONTHLY_PRICE_YEARS", "2"))

# 테스트용: 티커 수를 제한합니다 (0이면 전체)
TICKER_LIMIT = int(os.getenv("QUANT_TICKER_LIMIT", "0"))

# ── 팩터 모델 설정 ───────────────────────────────────────────
QVM_WEIGHTS = [1 / 3, 1 / 3, 1 / 3]   # quality, value, momentum

# 어느 팩터를 '섹터 안에서만' 비교할지 (쉼표로 구분).
#   all        quality,value,momentum — 지금까지의 동작
#   no_mom     quality,value          — 모멘텀만 전 종목 비교
#
# 왜 선택지가 필요한가: 2017~2026 백테스트에서 지수(시총상위200 시총가중)에
# 91%p 졌는데, 연도별로 보면 2017~2023은 7년 중 5년을 이겼고 2025~2026에만
# -54%p, -58%p로 무너졌습니다. 그 두 해는 소수 대형주가 지수를 끌어올린 해고,
# 모델은 모멘텀까지 섹터 중립으로 재서 '그 섹터가 통째로 오른다'는 정보를
# 스스로 지우고 있었습니다. 섹터 상한 25%는 봤더라도 담지 못하게 막았습니다.
SECTOR_NEUTRAL = os.getenv("QUANT_SECTOR_NEUTRAL", "all").strip().lower()

# 저변동성 팩터를 쓸지. 켜면 QVM → QVML (네 팩터 균등 1/4씩).
# QVM에는 '얼마나 흔들리는 종목인가'가 아예 없어서, 모델 안에 낙폭을 줄일
# 수단이 하나도 없었습니다. 백테스트 MDD -41.8%의 유일한 내부 레버입니다.
USE_LOWVOL = os.getenv("QUANT_USE_LOWVOL", "0").strip() not in ("", "0", "false")
QVML_WEIGHTS = [0.25, 0.25, 0.25, 0.25]   # quality, value, momentum, lowvol

# 종목 단위 추세 필터 — 종목이 자기 N일 이동평균 아래면 후보에서 제외.
# 0이면 끔. 전에 실패한 리스크 오버레이는 '시장 지수'로 전량 현금화하는
# 것이었고(시장이 월평균 +1.52% 오르는 동안 현금 보유), 이건 다릅니다.
# 시장은 그대로 100% 투자하고, 하락 추세인 '개별 종목'만 피합니다.
TREND_STOCK_MA = int(os.getenv("QUANT_TREND_STOCK_MA", "0"))

# 보유 기준 섹터 비중 상한 (0이면 끔).
# 기존 MAX_SECTOR_PCT는 '살 때' 종목 수만 제한합니다. 4등분으로 나눠 사고
# 순위 버퍼로 유지하다 보면 보유가 누적돼 상한을 넘습니다 — 캡 40%를 걸고도
# 실제 한 섹터가 54%까지 갔습니다. 이건 보유 비중 자체를 누릅니다.
MAX_SECTOR_WEIGHT = float(os.getenv("QUANT_MAX_SECTOR_WEIGHT", "0"))
# 실제로 살 종목 수. 백테스트에서 검증한 값과 같아야 합니다 — 다르면
# 검증한 것과 다른 걸 사게 됩니다. 9.5년 백테스트 결과:
#   30종목  비용 후 초과 -10.9%p   (신호를 못 담음)
#   100종목 비용 후 초과 +15.7%p   ← 채택
#   일간 리밸런싱은 턴오버 634%로 비용이 신호를 다 먹습니다. 월말로 유지하세요.
N_PORTFOLIO = int(os.getenv("QUANT_N_PORTFOLIO", "100"))

# ── 포트폴리오 제약 ──────────────────────────────────────────
# 한 섹터가 차지할 수 있는 최대 비중. 1.0이면 제한 없음(예전 동작).
# 섹터 중립 z점수를 써도 최종 순위는 전 종목 통합이라 특정 섹터가 쏠립니다.
MAX_SECTOR_PCT = float(os.getenv("QUANT_MAX_SECTOR_PCT", "0.25"))

# 최근 N거래일 평균 거래대금이 이 값 미만이면 제외 (0이면 필터 끔).
# 실제로 원하는 수량을 살 수 있는 종목만 남기기 위한 것입니다.
LIQUIDITY_WINDOW = int(os.getenv("QUANT_LIQUIDITY_WINDOW", "20"))
MIN_TURNOVER_KR = float(os.getenv("QUANT_MIN_TURNOVER_KR", "500000000"))   # 5억원
MIN_TURNOVER_US = float(os.getenv("QUANT_MIN_TURNOVER_US", "5000000"))     # 500만 달러

# ── 종목별 비중 방식 ─────────────────────────────────────────
# 지금까지는 100종목을 1%씩 똑같이 담았습니다(equal). 그 결과 9.5년 동안
# 삼성전자를 114회차 중 91회차나 들고 있었는데도 비중이 계속 1%였고,
# 코스피 지수에 +103% vs +224%로 졌습니다. 지수를 못 이긴 가장 큰 원인이
# 종목 선택이 아니라 '비중'이었을 수 있어서 방식을 바꿔 끼울 수 있게 했습니다.
#
#   equal   동일가중 — 지금까지의 방식, 비교 기준
#   mcap    시총가중 — 큰 회사를 크게. 지수에 가까워집니다
#   score   점수가중 — qvm 순위에 선형. 상위 종목에 더 싣습니다
#   invvol  역변동성 — 덜 흔들리는 종목을 크게. 낙폭·샤프 개선 목적
#
# MAX_WEIGHT는 한 종목 상한입니다. 0이면 방식별 기본값(_DEFAULT_CAP)을 씁니다.
# 시총가중에 상한이 없으면 삼성전자 하나가 20%를 넘어 '한 종목 베팅'이 됩니다.
WEIGHTING = os.getenv("QUANT_WEIGHTING", "equal")
MAX_WEIGHT = float(os.getenv("QUANT_MAX_WEIGHT", "0"))

# 역변동성 가중에서 변동성을 재는 기간(거래일).
VOL_WINDOW = int(os.getenv("QUANT_VOL_WINDOW", "60"))

# 무매매 밴드 — 보유 종목의 비중이 목표에서 이 비율 안으로만 벗어나 있으면
# 되돌리지 않습니다. 1%가 1.15%가 된 걸 매주 맞추면 거래세(0.20%)만 나갑니다.
# 실전(portfolio.py)이 ±20%로 동작하므로 백테스트도 같은 값을 써야 합니다.
# 0이면 매번 되맞춤 — 그 설정에서 회전율이 연 249%, 비용이 초과수익의 90%였습니다.
REBAL_BAND = float(os.getenv("QUANT_REBAL_BAND", "0.20"))

# 순위 버퍼 — 살 때는 상위 N_PORTFOLIO위, 팔 때는 그 (1+버퍼)배 밖으로
# 밀려나야 팝니다. 측정 결과 회전율의 89%가 종목 교체였고, 그 교체는 100위
# 경계를 오가는 종목들이었습니다(99위에 사고 103위에 팔고 98위에 되사기).
# 0.5면 "100위 안에서 사고 150위 밖에서 판다"입니다. 지수 제공사들이 편입·
# 제외에 쓰는 관례적인 방식이고, 좋은 값을 찾으려 여러 배수를 돌리지 않습니다.
RANK_BUFFER = float(os.getenv("QUANT_RANK_BUFFER", "0.5"))

# ── 실전 운용 자금 ───────────────────────────────────────────
# 주간 주문서에 '몇 주 살지'를 적으려면 총 평가액이 필요합니다.
# 종목당 배정액 = CAPITAL_KRW ÷ N_PORTFOLIO, 수량 = 배정액 ÷ 주가 (내림).
# 0이면 수량 칸을 비워 둡니다(금액은 직접 판단).
#
# 한국 주식은 1주 단위라서, 주가가 배정액보다 비싼 종목은 1주도 못 삽니다.
# 3,000만원 ÷ 100종목 = 종목당 30만원이므로 주가 30만원 초과 종목이 그렇습니다.
# 그런 종목은 주문서에서 빼고 다음 순위로 채웁니다 — 백테스트에는 없는
# 제약이라 실전 성과가 백테스트와 조금 벌어지는 원인 중 하나입니다.
CAPITAL_KRW = float(os.getenv("QUANT_CAPITAL_KRW", "0"))

# 리스크 오버레이에서 주식을 비웠을 때 현금에 붙는 이자(연). 0으로 두면
# 오버레이 효과를 과소평가하고, 높게 잡으면 과대평가합니다. 한국 예금금리
# 수준으로 보수적으로 잡았습니다.
CASH_RATE = float(os.getenv("QUANT_CASH_RATE", "0.02"))

# ── DART (과거 재무제표 확장) ────────────────────────────────
# FnGuide는 최근 분기만 줍니다. 백테스트 기간이 곧 통계적 신뢰도라
# (t = IR × √기간) DART로 2015년까지 끌어옵니다.
DART_API_KEY = os.getenv("DART_API_KEY", "")
DART_START_YEAR = int(os.getenv("QUANT_DART_START_YEAR", "2015"))
DART_DAILY_BUDGET = int(os.getenv("QUANT_DART_DAILY_BUDGET", "19000"))
DART_SLEEP = float(os.getenv("QUANT_DART_SLEEP", "0.25"))   # 요청 간격
# 연속 실패가 이만큼 쌓이면 중단합니다. 막힌 서버를 계속 두드리면
# 차단만 깊어지고 로그만 수만 줄로 불어납니다.
DART_MAX_FAILS = int(os.getenv("QUANT_DART_MAX_FAILS", "25"))
# 전 종목 x 11년이면 16만 회(8일)라 유동성 상위 N개만 받습니다.
# 백테스트에서 실제로 사는 건 거래대금 5억 이상 ~700종목이라 여유 있게 자릅니다.
DART_MAX_TICKERS = int(os.getenv("QUANT_DART_MAX_TICKERS", "1200"))
# DART 응답 한 건이 3~4초 걸립니다(전체 재무제표라 200행 안팎). 순차로 돌리면
# 하루 한도(2만 회)를 다 쓰기 전에 Actions 작업 시간(6시간)이 먼저 끝납니다.
DART_WORKERS = int(os.getenv("QUANT_DART_WORKERS", "3"))
# 올해치는 분기가 계속 새로 나오므로 이 일수가 지나면 다시 받습니다.
DART_REFRESH_DAYS = int(os.getenv("QUANT_DART_REFRESH_DAYS", "20"))
# 월간 실행에서 최신 분기를 받는 데 쓸 호출 수. 과거 채우기(하루 19,000)와
# 같은 날 겹치면 한도에 걸릴 수 있는데, 그때는 기존 재무제표로 그냥 돌아갑니다.
DART_MONTHLY_BUDGET = int(os.getenv("QUANT_DART_MONTHLY_BUDGET", "8000"))

# ── 에이전트 설정 ────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AGENT_MODEL = os.getenv("QUANT_AGENT_MODEL", "claude-sonnet-5")
MAX_RETRIES = int(os.getenv("QUANT_MAX_RETRIES", "2"))
RETRY_WAIT = int(os.getenv("QUANT_RETRY_WAIT", "30"))

# 데이터 이상 감지 임계값
MIN_TICKERS = int(os.getenv("QUANT_MIN_TICKERS", "1500"))
MAX_ERROR_RATE = float(os.getenv("QUANT_MAX_ERROR_RATE", "0.05"))
MIN_SECTORS = int(os.getenv("QUANT_MIN_SECTORS", "10"))

# ── 결과 엑셀 파일명 ─────────────────────────────────────────
# 예) "상수리 퀀트투자 알고리즘 미국 0913_100.xlsx"
REPORT_NAME = os.getenv("QUANT_REPORT_NAME", "상수리 퀀트투자 알고리즘")
REPORT_SUFFIX = os.getenv("QUANT_REPORT_SUFFIX", "100")


def report_filename(market_label: str, yyyymmdd: str) -> str:
    """market_label은 '한국' 또는 '미국', yyyymmdd는 8자리."""
    return f"{REPORT_NAME} {market_label} {yyyymmdd[4:]}_{REPORT_SUFFIX}.xlsx"


# ── 알림 설정 ────────────────────────────────────────────────
SMTP_HOST = os.getenv("QUANT_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("QUANT_SMTP_PORT", "587"))
SMTP_USER = os.getenv("QUANT_SMTP_USER", "")    # 보내는 Gmail 주소
SMTP_PASS = os.getenv("QUANT_SMTP_PASS", "")    # Gmail 앱 비밀번호 16자리
REPORT_EMAIL = os.getenv("QUANT_REPORT_EMAIL", "lovesoccers42@gmail.com")

TELEGRAM_BOT_TOKEN = os.getenv("QUANT_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("QUANT_TG_CHAT_ID", "")
