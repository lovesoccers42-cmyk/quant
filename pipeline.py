# -*- coding: utf-8 -*-
"""파이프라인 단계 정의 + 데이터 이상 감지 규칙."""
from dataclasses import dataclass, field
from typing import Callable

import config
import factor_global
import factor_kor
import global_fs
import global_price
import global_ticker
import global_value
import kor_fs
import kor_price
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
    if stats["sectors_ok"] < config.MIN_SECTORS:
        warns.append(f"WISE 섹터 {stats['sectors_ok']}/{config.MIN_SECTORS}개만 수집됨"
                     f" (실패: {stats['sectors_failed']})")
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
        Step("저장소 준비", store.init_db, critical=True),
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
        Step("저장소 준비", store.init_db, critical=True),
        Step("티커 수집(pykrx)", kor_ticker.collect, {"biz_day": biz_day},
             critical=True, validator=check_ticker),
        Step("섹터 수집(WISE)", kor_sector.collect, {"biz_day": biz_day},
             validator=check_sector),
        Step(f"주가 수집({config.MONTHLY_PRICE_YEARS}년)", kor_price.collect,
             {"years": config.MONTHLY_PRICE_YEARS}, critical=True, validator=check_crawl),
        Step("재무제표 수집(FnGuide)", kor_fs.collect, critical=True,
             validator=check_crawl),
        Step("밸류 지표 계산", kor_value.build),
        Step("QVM 팩터 모델", factor_kor.run, validator=check_factor),
    ]


# ── 미국장 ───────────────────────────────────────────────────
def us_daily_steps() -> list[Step]:
    return [
        Step("저장소 준비", store.init_db, critical=True),
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
        Step("저장소 준비", store.init_db, critical=True),
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
