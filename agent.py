# -*- coding: utf-8 -*-
"""지능형 에이전트 오케스트레이터.

1. 파이프라인 단계를 순차 실행하고 실패 시 자동 재시도
2. 단계별 이상 감지 규칙(validator)으로 데이터 품질 점검
3. 실행 결과를 Claude API에 넘겨 진단·요약 리포트 생성
4. 이메일(엑셀 첨부)/텔레그램으로 발송 + 대시보드용 결과 파일 기록
"""
import json
import logging
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime

import config
import notify
import store
from pipeline import Step, build_steps

log = logging.getLogger("quant_agent")

MARKET_NAME = {"kr": "한국장", "us": "미국장"}
MARKET_TABLES = {"kr": store.KOR_TABLES, "us": store.US_TABLES}


@dataclass
class StepResult:
    name: str
    status: str = "pending"        # ok / warn / fail / skipped
    attempts: int = 0
    duration_sec: float = 0.0
    stats: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: str = ""


def _run_step(step: Step) -> StepResult:
    res = StepResult(name=step.name)
    for attempt in range(1, config.MAX_RETRIES + 2):
        res.attempts = attempt
        t0 = time.time()
        try:
            stats = step.func(**step.kwargs) or {}
            res.duration_sec = round(time.time() - t0, 1)
            res.stats = {k: v for k, v in stats.items()
                         if k not in ("errors", "top_buys")}
            if "errors" in stats:
                res.stats["error_count"] = len(stats["errors"])
                res.stats["error_samples"] = stats["errors"][:10]
            if "top_buys" in stats:
                res.stats["top_buys"] = stats["top_buys"]

            if step.validator:
                res.warnings = step.validator(stats)
            res.status = "warn" if res.warnings else "ok"
            return res
        except Exception:
            res.duration_sec = round(time.time() - t0, 1)
            res.error = traceback.format_exc(limit=3)
            log.warning("[%s] %d차 시도 실패:\n%s", step.name, attempt, res.error)
            if attempt <= config.MAX_RETRIES:
                time.sleep(config.RETRY_WAIT)
    res.status = "fail"
    return res


def _fallback_report(label: str, results: list[StepResult]) -> str:
    lines = [f"[퀀트 에이전트] {label} 실행 결과 — {datetime.now():%Y-%m-%d %H:%M}", ""]
    for r in results:
        mark = {"ok": "[정상]", "warn": "[주의]", "fail": "[실패]", "skipped": "[생략]"}[r.status]
        lines.append(f"{mark} {r.name} ({r.attempts}회 시도, {r.duration_sec}s)")
        if r.stats:
            brief = {k: v for k, v in r.stats.items() if k != "top_buys"}
            lines.append(f"   {json.dumps(brief, ensure_ascii=False, default=str)}")
        for w in r.warnings:
            lines.append(f"   ! {w}")
        if r.error:
            lines.append(f"   오류: {r.error.splitlines()[-1]}")

    for r in results:
        buys = r.stats.get("top_buys", [])
        if buys:
            lines += ["", "── 매수 신호 상위 ──"]
            for row in buys:
                name = row.get("종목명") or row.get("Name")
                code = row.get("종목코드") or row.get("Symbol")
                extra = (f"{row.get('매수/매도')} · RSI {row.get('RSI')}"
                         if row.get("매수/매도") is not None else "")
                lines.append(f"   {name} ({code}) {extra} · qvm {row.get('qvm')}")
    return "\n".join(lines)


def _llm_report(label: str, results: list[StepResult]) -> str:
    import anthropic

    payload = [
        {"name": r.name, "status": r.status, "attempts": r.attempts,
         "duration_sec": r.duration_sec, "stats": r.stats,
         "warnings": r.warnings, "error": r.error}
        for r in results
    ]

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=config.AGENT_MODEL,
        max_tokens=1500,
        system=(
            "당신은 주식 퀀트 데이터 파이프라인을 감시하는 운영 에이전트입니다. "
            "이 파이프라인은 GitHub Actions(미국 서버 IP)에서 돌아갑니다. "
            "실행 결과 JSON을 보고 한국어 리포트를 작성하세요. 구성: "
            "1) 한 줄 종합 판정(정상/주의/실패) "
            "2) 단계별 결과 요약 "
            "3) 오류·경고가 있으면 원인 진단과 구체적 조치 제안 "
            "(한국장은 네이버·FnGuide 차단, 미국장은 Yahoo의 데이터센터 IP 제한을 "
            "우선 의심하고 묶음 크기·간격 조정을 제안) "
            "4) top_buys가 있으면 상위 종목 표 형태 요약. "
            "간결하게, 불필요한 수식어 없이."
        ),
        messages=[{
            "role": "user",
            "content": f"실행: {label}\n실행 결과:\n"
                       f"{json.dumps(payload, ensure_ascii=False, default=str)}",
        }],
    )
    return msg.content[0].text


def run(mode: str = "daily", market: str = "kr") -> int:
    label = f"{MARKET_NAME.get(market, market)} {mode}"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(
                config.LOG_DIR / f"{market}_{mode}_{datetime.now():%Y%m%d}.log",
                encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )

    started = datetime.now()

    try:
        steps = build_steps(market, mode)
    except Exception:
        tb = traceback.format_exc(limit=3)
        log.error("파이프라인 구성 실패:\n%s", tb)
        notify.send_report(f"[퀀트 에이전트] {label} 실패 — {datetime.now():%m/%d}",
                           f"파이프라인 구성 단계에서 오류 발생:\n{tb}")
        return 1

    results: list[StepResult] = []
    aborted = False

    for step in steps:
        if aborted:
            results.append(StepResult(name=step.name, status="skipped"))
            continue

        log.info("▶ %s 시작", step.name)
        res = _run_step(step)
        results.append(res)
        log.info("  %s → %s %s", step.name, res.status,
                 json.dumps(res.stats, ensure_ascii=False, default=str)[:300])

        if res.status == "fail" and step.critical:
            log.error("치명적 단계 실패 → 이후 단계 중단")
            aborted = True

    # 리포트 생성
    try:
        report = (_llm_report(label, results) if config.ANTHROPIC_API_KEY
                  else _fallback_report(label, results))
    except Exception as e:
        log.warning("LLM 리포트 생성 실패(%s) → 기본 리포트 사용", e)
        report = _fallback_report(label, results)

    log.info("리포트:\n%s", report)

    has_fail = any(r.status == "fail" for r in results)
    has_warn = any(r.status == "warn" for r in results)
    status_txt = "실패" if has_fail else ("주의" if has_warn else "정상")
    subject = f"[퀀트 에이전트] {label} {status_txt} — {datetime.now():%m/%d}"

    # 대시보드가 읽을 실행 요약
    summary = {
        "market": market,
        "mode": mode,
        "status": status_txt,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().isoformat(timespec="seconds"),
        "minutes": round((datetime.now() - started).total_seconds() / 60, 1),
        "report": report,
        "steps": [asdict(r) for r in results],
        "store": store.summary(MARKET_TABLES.get(market)),
    }
    (config.OUTPUT_DIR / f"run_{market}_latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, default=str, indent=2), encoding="utf-8")

    # 엑셀 첨부해서 메일 발송
    attachments = []
    for r in results:
        xl = r.stats.get("excel")
        if xl:
            attachments.append(xl)

    sent = notify.send_report(subject, report, attachments)
    log.info("알림 발송: %s", sent or "설정된 채널 없음(로그만 기록)")

    return 1 if has_fail else 0


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    market = sys.argv[2] if len(sys.argv) > 2 else "kr"
    sys.exit(run(mode, market))
