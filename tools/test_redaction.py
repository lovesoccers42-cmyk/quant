# -*- coding: utf-8 -*-
"""인증키가 로그·실행 요약으로 새지 않는가.

이 저장소는 공개입니다. Actions 로그·아티팩트·릴리스 파일은 누구나 받을
수 있고, DART는 인증키를 URL 쿼리(?crtfc_key=...)로 받습니다. 실제로 한 번
새었고(2026-10 이전 아티팩트), 그 뒤 마스킹을 넣었지만 아래 모양들은
필터를 그대로 통과했습니다.

  1. log.warning("실패: %s", e)   — 예외 '객체'를 인자로 넘김
  2. log.exception(...)            — traceback에 URL이 실림
  3. basicConfig(force=True)       — 핸들러를 갈아 끼우면 필터가 사라짐
  4. traceback 문자열을 JSON에 담아 공개 릴리스에 올림 (agent.py)

GitHub의 자동 가림은 Secrets의 '전체 값'이 Actions 화면 로그에 찍힐 때만
동작합니다. 우리가 만든 로그 파일(아티팩트)과 릴리스 파일은 가리지 않습니다.
"""
import io
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_util  # noqa: E402

FAKE = "0123456789abcdef0123456789abcdef01234567"   # 가짜 40자 키
URL = (f"https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?"
       f"crtfc_key={FAKE}&corp_code=00126380&bsns_year=2025")
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global FAIL
    print(("[OK ] " if cond else "[실패] ") + msg)
    if not cond:
        FAIL += 1


def capture(fn) -> str:
    buf = io.StringIO()
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(buf)], force=True)
    http_util.install_log_redaction()
    fn()
    for h in logging.getLogger().handlers:
        h.flush()
    return buf.getvalue()


log = logging.getLogger("quant_agent.test_redaction")


class Boom(Exception):
    pass


print("[로그 — 모양과 상관없이 가리는가]")
out = capture(lambda: log.warning("요청 실패 %s", URL))
check(FAKE not in out and "crtfc_key=***" in out, "문자열 인자")

out = capture(lambda: log.warning("요청 실패 %s", Boom(f"Max retries with url: {URL}")))
check(FAKE not in out, "예외 객체 인자 (예전 필터가 통과시킨 모양)")


def _raise_and_log():
    try:
        raise Boom(f"HTTPSConnectionPool: Max retries exceeded with url: {URL}")
    except Boom:
        log.exception("수집 실패")


out = capture(_raise_and_log)
check(FAKE not in out and "Traceback" in out, "log.exception의 traceback")

# 핸들러를 다시 만들어도(force=True) 유지되는가 — 이번엔 install을 다시 안 부름
buf = io.StringIO()
logging.basicConfig(level=logging.INFO, format="%(message)s",
                    handlers=[logging.StreamHandler(buf)], force=True)
log.warning("재설정 뒤 %s", Boom(URL))
check(FAKE not in buf.getvalue(), "basicConfig(force=True)로 핸들러를 갈아도 유지")

print("\n[로그 밖 — 실행 요약·리포트로 나가는 문자열]")
check(FAKE not in http_util.redact(f"Traceback ...\n  {URL}\n"), "redact()가 traceback 문자열을 가림")
check(http_util.redact(None) is None and http_util.redact(3) == 3,
      "문자열이 아니면 그대로 돌려줌")
check(http_util.redact("a=b 일반 문장") == "a=b 일반 문장", "비밀값이 없으면 손대지 않음")

print()
if FAIL:
    print(f"실패 {FAIL}건 — 인증키가 공개 로그로 샐 수 있습니다.")
    sys.exit(1)
print("전체 통과 — 인증키가 로그·요약에 남지 않습니다.")
