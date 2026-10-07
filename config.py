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
CAPITAL_KRW = float(os.getenv("QUANT_CAPITAL_KRW", "0"))

# 배정액의 몇 배까지는 '1주만' 담을지. 1 이하면 끕니다(= 비싼 종목 제외).
#
# 왜 1이 아니라 2인가: 1,000만원이면 종목당 10만원이라 상위 100종목 중 평균
# 9종목이 주가 때문에 빠집니다. 빠진 자리를 다음 순위로 채우면 '비싼 주식은
# 안 사는' 전략이 되는데, 백테스트에는 없는 제약입니다. 2024~2025 7개 시점
# 실측(tools 아래 측정 스크립트):
#   제외하고 채움  : 제약 없는 포트폴리오보다 12개월 -4.26%p (7/7 시점 모두 낮음)
#   2배까지 1주 허용: +0.67%p (표준편차 4.16%p) — 사실상 같음
#   3배까지 허용   : +4.85%p. 다만 비싼 9종목에 전체의 15~20%가 실립니다.
#                    그건 '비싼 주식에 베팅'이라 2배에서 멈춥니다.
LOT_TOLERANCE = float(os.getenv("QUANT_LOT_TOL", "2.0"))

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
# 예) "상수리 퀀트투자 알고리즘 미국 us-0913_100.xlsx"
#
# 파일명에 영문 토큰(us-, kr-, weekly-main- 같은)을 꼭 넣습니다. GitHub 릴리스는
# 에셋 이름에서 한글을 떼어내기 때문에, 토큰이 없으면 서로 다른 리포트가 모두
# "0913_100.xlsx" 하나로 합쳐지고 --clobber로 덮어써집니다. 실제로 10-06에
# 주간 주문서가 같은 날 돌아간 백테스트 리포트에 덮여 사라졌습니다.
REPORT_NAME = os.getenv("QUANT_REPORT_NAME", "상수리 퀀트투자 알고리즘")
REPORT_SUFFIX = os.getenv("QUANT_REPORT_SUFFIX", "100")


def report_filename(market_label: str, yyyymmdd: str, token: str = "") -> str:
    """market_label은 '한국' 또는 '미국', yyyymmdd는 8자리.

    token은 파일명에 남길 영문 식별자입니다(릴리스에서 한글이 떨어져도 구분되게).
    """
    tok = f"{token}-" if token else ""
    return f"{REPORT_NAME} {market_label} {tok}{yyyymmdd[4:]}_{REPORT_SUFFIX}.xlsx"


# ── 알림 설정 ────────────────────────────────────────────────
SMTP_HOST = os.getenv("QUANT_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("QUANT_SMTP_PORT", "587"))
SMTP_USER = os.getenv("QUANT_SMTP_USER", "")    # 보내는 Gmail 주소
SMTP_PASS = os.getenv("QUANT_SMTP_PASS", "")    # Gmail 앱 비밀번호 16자리
REPORT_EMAIL = os.getenv("QUANT_REPORT_EMAIL", "lovesoccers42@gmail.com")

TELEGRAM_BOT_TOKEN = os.getenv("QUANT_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("QUANT_TG_CHAT_ID", "")

# ── 포트폴리오 프로필 ────────────────────────────────────────
# 두 사람이 같은 엔진을 쓰되 '성격이 다른 설정'으로 돌립니다.
#
# 왜 다르게 하나: 종목을 달리해도 둘 다 한국 주식 100% 롱이라 상관계수가
# 0.9 안팎입니다. 분산 효과는 작습니다. 진짜 이유는 지금 main 설정이 측정
# 13번 끝에 고른 값이라는 것 — 성격이 다른 설정을 병행하면 "그 설정이 과거에만
# 맞았다"는 위험을 반만 지게 됩니다. 그리고 같은 소형주를 두 계좌가 같은 날
# 사면 서로 호가를 밀어올립니다.
#
#   main  점수가중 + 모멘텀 섹터중립 해제 + 섹터캡 없음
#         → 신호 순위에 싣고, 섹터 로테이션을 허용. 집중도 높음(한 섹터 44%)
#   alt   역변동성가중 + 전 팩터 섹터중립 + 섹터캡 25%
#         → 변동성으로 싣고, 섹터 로테이션 안 함. 쏠림 없음
#
# 두 축(비중 기준 · 섹터 태도)이 정반대입니다. slot_offset으로 손보는 등분을
# 엇갈리게 해서 같은 주에 같은 종목을 동시에 거래하지 않게 합니다.
PROFILES: dict[str, dict] = {
    # split: 같은 모델의 순위 명단을 번갈아 나눠 갖습니다.
    #   {"mod": 2, "rem": 0} = 1,3,5,7...위   {"mod": 2, "rem": 1} = 2,4,6,8...위
    # 이게 '종목만 다르게'를 가장 정직하게 만드는 방법입니다. 두 번째로 잘 나오는
    # 설정을 찾아 헤매면 그건 과거에 맞추는 일이고, 교차 분할은 검증된 같은
    # 신호에서 뽑으므로 기대수익이 구조적으로 같습니다.
    #
    # 대가: 둘 다 상위 200위까지 손을 벌려야 100종목을 채웁니다(혼자면 100위까지).
    # 101~200위가 섞이는 만큼 양쪽 다 조금 희석됩니다 — 아내분만이 아니라
    # 상수님 쪽도요. 그 희석이 얼마인지는 돌려봐야 압니다.
    "main": {
        "라벨": "상수",
        "weighting": "score",
        "sector_neutral": "no_mom",
        "max_sector_pct": 1.0,
        "slot_offset": 0,
        "qvm_weights": None,          # None이면 config.QVM_WEIGHTS
        # shared = 두 계좌가 함께 보는 종목 비중. 0.7이면 보유 종목의 약 80%가
        # 공통이고 20%만 각자 전용입니다. 왜 중간을 쓰나: 완전 분할(shared 0)은
        # 두 계좌 성과를 운에 맡깁니다 — 같은 전략의 두 바구니가 9.5년에 91.6%p
        # 벌어졌습니다. 공용 비중을 키우면 그 분산이 전용 비중에 비례해 줄고,
        # 더 넓은 풀에서 고르니 희석도 줄어듭니다.
        # 공용 70% + 전용 30%. 보유 종목의 약 81개가 두 계좌 공통, 19개가 전용.
        #
        # 측정된 양 끝:
        #   분할 없음(공용 100%)  합계 151.09%
        #   완전 분할(공용 0%)    상수 175.53% / 배우자 83.89% · 합계 126.25%
        # 공용 70%는 그 사이의 보간이고 직접 측정하지는 않았습니다.
        # 희석은 완전 분할의 30% 수준(약 7~8%p)으로 예상되지만 예상입니다.
        # 2/0@0.7 · 2/1@0.7 백테스트 2회로 확인할 수 있습니다.
        "split": {"mod": 2, "rem": 0, "shared": 0.7},
    },
    # alt는 main과 '설정이 같고 종목만 다른' 계좌입니다.
    #
    # 성격이 다른 설정을 찾아보려 했지만 전부 더 나빴습니다 — 역변동성 가중은
    # 전종목 동일가중에도 -5.32%p로 졌고(t -0.27), 전 팩터 섹터중립+캡25%는
    # +11.30%p(t 0.25)로 main의 +60.79%p(t 1.12)에 크게 못 미쳤습니다.
    # 두 번째로 잘 나오는 설정을 더 찾는 것은 과거에 맞추는 일이므로 멈췄습니다.
    #
    # 대신 검증된 같은 모델에서 종목만 나눕니다. 코드 해시로 고정 배정하므로
    # 보유가 구조적으로 0% 겹치고, 같은 신호에서 뽑으므로 기대수익이 같습니다.
    # 대가는 둘 다 자기 바구니(약 350종목)에서 100종목을 채워야 해서 양쪽 다
    # 조금 희석된다는 것 — 상수님 쪽도 포함입니다.
    "alt": {
        "라벨": "배우자",
        "weighting": "score",
        "sector_neutral": "no_mom",
        "max_sector_pct": 1.0,
        "slot_offset": 2,
        "qvm_weights": None,
        "split": {"mod": 2, "rem": 1, "shared": 0.7},
    },
}
DEFAULT_PROFILE = os.getenv("QUANT_PROFILE", "main").strip().lower()


def profile(name: str | None = None) -> dict:
    """프로필 설정을 돌려줍니다. 모르는 이름은 조용히 main으로 넘기지 않습니다."""
    key = (name or DEFAULT_PROFILE or "main").strip().lower()
    if key not in PROFILES:
        raise ValueError(f"모르는 프로필 '{key}'. 쓸 수 있는 값: {list(PROFILES)}")
    out = dict(PROFILES[key])
    out.setdefault("qvm_weights", None)
    out.setdefault("split", None)
    out["key"] = key
    # 프로필별 테이블·파일 이름 — main은 기존 이름을 그대로 써서 호환됩니다
    suffix = "" if key == "main" else f"_{key}"
    out["state_table"] = f"kor_portfolio{suffix}"
    out["capital_table"] = f"kor_capital{suffix}"
    out["model_file"] = f"model_kr{suffix}_latest.parquet"
    out["hold_file"] = f"model_kr{suffix}_hold.parquet"
    return out

# ── 확정 설정 (한국) ─────────────────────────────────────────
# 2026-10 확정. 백테스트 2017-03-31~2026-10-02, 495회차 기준:
#   누적 151.09% · 연 10.15% · 샤프 0.46 · MDD -42.74% · 회전율 연 153%
#   전종목 동일가중 대비 +60.79%p (t 1.12)
#   유동성통과 대비 연초과 +6.68%p (t 2.38)
#   시총상위200 지수 대비 -46.66%p  ← 지수는 못 이깁니다
#
# 꼬리표 (지우지 마세요):
#   · 생존편향 — 상장폐지 종목이 데이터에 없어 성과가 과대평가됩니다
#   · 다중검정 20회 이상 — t값은 보정 전입니다
#   · 수익과 무관한 기준으로 종목을 반 가르니 두 쪽이 91.6%p 벌어졌습니다.
#     즉 +60.79%p는 그 잡음 폭 안에 있습니다
#
# run_backtest.py는 market="kr"이고 인자를 안 주면 이 값을 씁니다.
# 미국장(us)은 영향받지 않습니다.
CONFIRMED_KR: dict = {
    "top_n": N_PORTFOLIO,       # 100 — 30은 -10.9%p, 150은 더 나빴습니다
    "rebalance": "W-FRI",
    "cost_bps": 25.0,
    "max_sector_pct": 1.0,      # 섹터 상한 해제 (모멘텀 섹터중립을 풀었으므로)
    "min_turnover": MIN_TURNOVER_KR,
    "tranches": 4,
    "trend_ma": 0,              # 시장 타이밍 오버레이 — 측정 결과 실패
    "risk_off": 0.0,
    "weighting": "score",
    "max_weight": 0.0,          # 방식별 기본값(score 3%)
    "rebal_band": 0.20,
    "rank_buffer": 0.5,
    "sector_neutral": "no_mom",
    "use_lowvol": 0,            # 측정 결과 MDD가 더 나빠져 기각
    "trend_stock": 0,           # 측정 결과 회전율만 올려 기각
    "max_sector_weight": 0.0,
    "qvm_weights": None,        # 1/3씩
}

# ── 미국장 검증 설정 ────────────────────────────────────────
# 한국 확정 설정과 '구조'를 똑같이 맞춘 값입니다. 목적은 수익이 아니라 검증 —
# 한국 데이터를 20번 넘게 돌려 얻은 결과가, 한 번도 건드리지 않은 미국
# 데이터에서도 재현되는지 보는 것입니다. 재현되면 메커니즘이 실재할 가능성이
# 크고, 안 되면 한국 9.5년에 맞춘 것일 확률이 큽니다.
#
# 구조(종목수·주기·등분·비중·버퍼·밴드·섹터중립)는 한국과 동일하게 둡니다.
# 시장마다 다를 수밖에 없는 것만 바꿉니다:
#   · cost_bps 25 → 10 : 한국은 증권거래세 0.20%가 매도에 붙지만 미국은
#     없습니다. 한국 기준을 그대로 쓰면 미국 쪽을 부당하게 불리하게 만듭니다.
#   · min_turnover : 5억원 → 500만 달러
#   · 섹터중립 no_mom 유지 — 한국에서 섹터 로테이션이 핵심이었으므로
#
# ※ 비교할 때는 '비용 전 초과수익'과 IC를 먼저 보세요. 비용 가정이 시장마다
#   다르므로 비용 후 숫자만으로 두 시장을 견주면 가정 차이를 실력 차이로
#   잘못 읽습니다.
CONFIRMED_US: dict = {
    "top_n": N_PORTFOLIO,
    "rebalance": "W-FRI",
    "cost_bps": 10.0,
    "max_sector_pct": 1.0,
    "min_turnover": MIN_TURNOVER_US,
    "tranches": 4,
    "trend_ma": 0,
    "risk_off": 0.0,
    "weighting": "score",
    "max_weight": 0.0,
    "rebal_band": 0.20,
    "rank_buffer": 0.5,
    "sector_neutral": "no_mom",
    "use_lowvol": 0,
    "trend_stock": 0,
    "max_sector_weight": 0.0,
    "qvm_weights": None,
}
