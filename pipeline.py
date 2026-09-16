# -*- coding: utf-8 -*-
"""파이프라인 단계 정의 + 데이터 이상 감지 규칙."""
from dataclasses import dataclass, field
from typing import Callable

from datetime import date

import pandas as pd

import config
import factor_global
import factor_kor
import global_fs
import global_price
import global_ticker
import global_value
import kor_fs
import kor_price
import kor_fs_dart
import kor_sector
import kor_ticker
import kor_value
import store


@dataclass
class Step:
    name: str
    func: Callable[..., dict]
    kwargs: dict = field(default_factory=dict)
    critical: bool = False
    validator: Callable[[dict], list] | None = None


# ── 이상 감지 규칙 ───────────────────────────────────────────
def check_ticker(stats: dict) -> list:
    warns = []
    if stats["rows"] < config.MIN_TICKERS:
        warns.append(f"티커 수 {stats['rows']}개 — 평소보다 급감 (기준 {config.MIN_TICKERS})")
    if stats["common_stocks"] < config.MIN_TICKERS * 0.6:
        warns.append(f"보통주 수 {stats['common_stocks']}개 — 비정상적으로 적음")
    return warns


def check_sector(stats: dict) -> list:
    warns = []
    if stats.get("기존섹터사용"):
        # 새로 못 받았지만 기존 섹터로 계속 돌아갑니다. 섹터는 거의 안 바뀌므로
        # 당장은 문제가 없지만, 오래 방치하면 신규 상장·섹터 변경이 누락됩니다.
        warns.append(f"WISE 수집 실패 — 기존 섹터({stats['기존섹터사용']}) 사용 중. "
                     f"사유: {stats.get('fail_reasons')}")
        return warns
    if stats["sectors_ok"] < config.MIN_SECTORS:
        warns.append(f"WISE 섹터 {stats['sectors_ok']}/{config.MIN_SECTORS}개만 수집됨"
                     f" (실패: {stats['sectors_failed']})")
    return warns


def check_fs_dart(stats: dict) -> list:
    """DART 재무제표 최신화 점검.

    호출 한도에 걸린 건 실패가 아닙니다 — 과거 채우기 작업이 그날 한도를 먼저
    쓴 경우이고, 기존 재무제표로 모델은 그대로 돌아갑니다. 정말 위험한 건
    저장된 재무제표가 오래돼서 낡은 숫자로 종목을 고르는 상황입니다.
    """
    warns = []
    why = stats.get("중단사유", "")
    if why and why != "완료" and stats.get("저장행수", 0) == 0:
        warns.append(f"DART 최신화 못 함({why}) — 기존 재무제표로 진행합니다")

    try:
        latest = store.read_sql(
            "select max(기준일) 최신 from kor_fs where 공시구분 = 'q';")
        last = pd.to_datetime(latest.iloc[0]["최신"])
        age = (pd.Timestamp.today().normalize() - last).days
        if age > 150:
            warns.append(f"재무제표가 {age}일 전({last:%Y-%m-%d})까지만 있습니다 — "
                         f"분기 공시가 안 들어오고 있습니다")
    except Exception as e:
        warns.append(f"재무제표 최신 시점을 확인하지 못함: {e}")
    return warns


def check_crawl(stats: dict) -> list:
    warns = []
    if stats.get("error_rate", 0) > config.MAX_ERROR_RATE:
        warns.append(f"수집 실패율 {stats['error_rate']:.1%} — 기준({config.MAX_ERROR_RATE:.0%}) 초과, "
                     f"실패 {len(stats.get('errors', []))}종목.")
    if stats.get("rows", 0) == 0:
        warns.append("저장된 행이 0건 — 소스 구조 변경 또는 차단 가능성")
    if stats.get("fail_reasons"):
        warns.append(f"실패 사유: {stats['fail_reasons']}")
    return warns


def check_store(stats: dict) -> list:
    """저장소에 빠진 테이블이 있으면 바로 짚어 줍니다."""
    missing = stats.get("missing") or []
    if not missing:
        return []
    return [f"저장소에 없는 테이블: {missing}. 릴리스(data-store)에 해당 parquet이 "
            f"올라가 있는지 확인하세요. 비어 있으면 뒤 단계가 0건으로 돌아갑니다."]


def check_factor(stats: dict) -> list:
    warns = []
    if stats["selected"] < config.N_PORTFOLIO * 0.5:
        warns.append(f"선정 종목 {stats['selected']}개 — 목표({config.N_PORTFOLIO})의 절반 미만")

    # 어느 팩터에서 끊겼는지 짚어 줍니다 (qvm은 세 z가 모두 있어야 나옵니다)
    cov = stats.get("coverage") or {}
    if cov and cov.get("qvm", 0) == 0:
        empty = [k for k in ("z_quality", "z_value", "z_momentum") if cov.get(k, 0) == 0]
        warns.append(
            f"qvm 점수를 받은 종목이 0개입니다. 비어 있는 팩터: {empty or '없음'} "
            f"(커버리지 {cov}). 해당 팩터의 원천 수집 단계를 먼저 확인하세요.")
    return warns


def check_us_ticker(stats: dict) -> list:
    warns = []
    if stats["us_stocks"] < 2000:
        warns.append(f"미국 종목 {stats['us_stocks']}개 — 평소보다 적음 "
                     f"(거래소 실패: {stats['exchanges_failed']})")
    if stats.get("source") == "csv":
        warns.append("nasdaq.com API가 막혀 CSV 대체 경로로 수집했습니다 — "
                     "CSV가 오래되면 신규 상장이 빠집니다.")
    return warns


def check_us_crawl(stats: dict) -> list:
    warns = check_crawl(stats)
    if stats.get("error_rate", 0) > config.MAX_ERROR_RATE:
        warns.append("Yahoo가 GitHub 서버 IP를 제한했을 수 있습니다 — "
                     "묶음 크기(QUANT_YF_CHUNK)를 줄이거나 간격을 늘려보세요.")
    return warns


# ── 실행 계획 ────────────────────────────────────────────────
def _biz_day():
    try:
        return kor_ticker.latest_business_day()
    except Exception:
        from datetime import date, timedelta
        d = date.today()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")


def daily_steps() -> list[Step]:
    biz_day = _biz_day()
    return [
        Step("저장소 준비", store.init_db, {"market": "kr"},
             critical=True, validator=check_store),
        Step("티커 수집(pykrx)", kor_ticker.collect, {"biz_day": biz_day},
             critical=True, validator=check_ticker),
        Step("섹터 수집(WISE)", kor_sector.collect, {"biz_day": biz_day},
             validator=check_sector),
        Step(f"주가 수집(최근 {config.DAILY_PRICE_DAYS}일)", kor_price.collect,
             {"days": config.DAILY_PRICE_DAYS}, critical=True, validator=check_crawl),
        Step("밸류 지표 계산", kor_value.build),
        Step("QVM 팩터 모델", factor_kor.run, critical=True, validator=check_factor),
    ]


def monthly_steps() -> list[Step]:
    biz_day = _biz_day()
    return [
        Step("저장소 준비", store.init_db, {"market": "kr"},
             critical=True, validator=check_store),
        Step("티커 수집(pykrx)", kor_ticker.collect, {"biz_day": biz_day},
             critical=True, validator=check_ticker),
        Step("섹터 수집(WISE)", kor_sector.collect, {"biz_day": biz_day},
             validator=check_sector),
        Step(f"주가 수집({config.MONTHLY_PRICE_YEARS}년)", kor_price.collect,
             {"years": config.MONTHLY_PRICE_YEARS}, critical=True, validator=check_crawl),
        # FnGuide는 2026-09 기준 해당 URL이 사라졌습니다("페이지가 없습니다").
        # 긁어오는 2차 출처라 이번을 포함해 두 번 깨졌습니다. 원출처인 DART로
        # 바꿨습니다 — 백테스트 과거 확장에 쓰는 바로 그 수집기입니다.
        # (kor_fs.py는 지워두지 않았습니다. FnGuide가 살아나면 다시 쓸 수 있습니다.)
        Step("재무제표 최신화(DART)", kor_fs_dart.collect,
             {"years": [date.today().year], "budget": config.DART_MONTHLY_BUDGET},
             validator=check_fs_dart),
        Step("밸류 지표 계산", kor_value.build),
        Step("QVM 팩터 모델", factor_kor.run, validator=check_factor),
    ]


# ── 미국장 ───────────────────────────────────────────────────
def us_daily_steps() -> list[Step]:
    return [
        Step("저장소 준비", store.init_db, {"market": "us"},
             critical=True, validator=check_store),
        Step("종목 수집(나스닥 스크리너)", global_ticker.collect,
             critical=True, validator=check_us_ticker),
        Step(f"주가 수집(최근 {config.US_DAILY_PRICE_DAYS}일)", global_price.collect,
             {"days": config.US_DAILY_PRICE_DAYS}, critical=True,
             validator=check_us_crawl),
        Step("밸류 지표 계산", global_value.build),
        Step("QVM 팩터 모델", factor_global.run, critical=True, validator=check_factor),
    ]


def us_monthly_steps() -> list[Step]:
    return [
        Step("저장소 준비", store.init_db, {"market": "us"},
             critical=True, validator=check_store),
        Step("종목 수집(나스닥 스크리너)", global_ticker.collect,
             critical=True, validator=check_us_ticker),
        Step(f"주가 수집({config.US_MONTHLY_PRICE_YEARS}년)", global_price.collect,
             {"years": config.US_MONTHLY_PRICE_YEARS}, critical=True,
             validator=check_us_crawl),
        Step("재무제표 수집(yahooquery)", global_fs.collect, critical=True,
             validator=check_us_crawl),
        Step("밸류 지표 계산", global_value.build),
        Step("QVM 팩터 모델", factor_global.run, validator=check_factor),
    ]


STEP_PLANS = {
    ("kr", "daily"): daily_steps,
    ("kr", "monthly"): monthly_steps,
    ("us", "daily"): us_daily_steps,
    ("us", "monthly"): us_monthly_steps,
}


def build_steps(market: str, mode: str) -> list[Step]:
    try:
        return STEP_PLANS[(market, mode)]()
    except KeyError:
        raise ValueError(f"알 수 없는 실행 조합: market={market}, mode={mode}")
