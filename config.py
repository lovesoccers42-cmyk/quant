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
# 팩터 모델은 최근 1년 주가만 사용합니다. 2년치만 보관해 저장소를 가볍게 유지.
PRICE_KEEP_YEARS = int(os.getenv("QUANT_PRICE_KEEP_YEARS", "2"))

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
N_PORTFOLIO = int(os.getenv("QUANT_N_PORTFOLIO", "1000"))

# ── 포트폴리오 제약 ──────────────────────────────────────────
# 한 섹터가 차지할 수 있는 최대 비중. 1.0이면 제한 없음(예전 동작).
# 섹터 중립 z점수를 써도 최종 순위는 전 종목 통합이라 특정 섹터가 쏠립니다.
MAX_SECTOR_PCT = float(os.getenv("QUANT_MAX_SECTOR_PCT", "0.25"))

# 최근 N거래일 평균 거래대금이 이 값 미만이면 제외 (0이면 필터 끔).
# 실제로 원하는 수량을 살 수 있는 종목만 남기기 위한 것입니다.
LIQUIDITY_WINDOW = int(os.getenv("QUANT_LIQUIDITY_WINDOW", "20"))
MIN_TURNOVER_KR = float(os.getenv("QUANT_MIN_TURNOVER_KR", "500000000"))   # 5억원
MIN_TURNOVER_US = float(os.getenv("QUANT_MIN_TURNOVER_US", "5000000"))     # 500만 달러

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
