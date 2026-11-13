# -*- coding: utf-8 -*-
"""공용 HTTP 세션.

해외 IP(GitHub Actions 러너)에서 네이버/FnGuide/WISE에 접근할 때
기본 User-Agent로는 차단되는 경우가 많아 브라우저 헤더를 붙입니다.
"""
import logging
import re
import threading
import time
import traceback

import requests as rq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

# ── 로그에서 비밀값 가리기 ───────────────────────────────────
# urllib3은 재시도할 때 요청 URL을 경고로 찍습니다. DART는 인증키를 쿼리
# 스트링으로 받기 때문에, 연결이 불안정하면 인증키가 로그에 수천 번 남습니다.
# 그 로그는 Actions 아티팩트로 보관돼 저장소를 볼 수 있는 사람에게 노출됩니다.
# 실제로 한 번 새어나갔습니다. 모든 로그 기록에서 지우고 갑니다.
_SECRET = re.compile(
    r"((?:crtfc_key|api_key|apikey|token|access_token|auth|pw|password)=)[^&\s\"']+",
    re.IGNORECASE)


def redact(text):
    """문자열 안의 인증키를 ***로 바꿉니다. 문자열이 아니면 그대로 돌려줍니다.

    로그 말고도 비밀값이 새는 길이 있습니다 — 실행 요약 JSON(공개 릴리스에
    올라갑니다), 메일·텔레그램 리포트, LLM 리포트 입력. 거기 들어가는
    오류 문자열(traceback)은 이 함수를 거쳐야 합니다.
    """
    if isinstance(text, str) and "=" in text:
        return _SECRET.sub(r"\1***", text)
    return text


def _redact_record(record: logging.LogRecord) -> None:
    """한 로그 기록을 **완성된 문장 기준으로** 가립니다.

    예전 필터는 msg와 '문자열' 인자만 봤습니다. 그래서
        log.warning("실패: %s", e)          # e = requests 예외 객체
        log.exception("...")                 # traceback
    처럼 예외 객체나 traceback에 실린 URL(…?crtfc_key=…)은 그대로
    통과했습니다. requests의 ConnectionError 메시지에는 요청 URL이 통째로
    들어 있습니다. 완성된 메시지와 traceback 문자열을 가리면 모양과 상관없이
    막힙니다.
    """
    try:
        msg = record.getMessage()
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(
                traceback.format_exception(*record.exc_info)).rstrip("\n")
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
    except Exception:
        pass


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


_FACTORY_INSTALLED = False


def install_log_redaction():
    """비밀값 마스킹을 겁니다. 로깅 설정 뒤에 부르세요.

    핸들러 필터만으로는 부족합니다 — 뒤에서 `logging.basicConfig(force=True)`
    를 다시 부르면(run_weekly·run_us_monthly_orders의 하위 명령들이 그렇게
    합니다) 핸들러가 새로 만들어지면서 필터가 사라집니다. 그래서 **기록이
    만들어지는 순간**(LogRecordFactory)에도 가립니다. 핸들러를 몇 번
    갈아 끼워도 유지됩니다.
    """
    global _FACTORY_INSTALLED
    f = RedactSecrets()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    for name in ("urllib3", "urllib3.connectionpool", "requests"):
        logging.getLogger(name).addFilter(f)
    if not _FACTORY_INSTALLED:
        base = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            rec = base(*args, **kwargs)
            _redact_record(rec)
            return rec

        logging.setLogRecordFactory(factory)
        _FACTORY_INSTALLED = True

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

_local = threading.local()


def session() -> rq.Session:
    """스레드별 재사용 세션 (커넥션 풀 + 자동 재시도)."""
    s = getattr(_local, "session", None)
    if s is not None:
        return s

    s = rq.Session()
    s.headers.update(BASE_HEADERS)
    retry = Retry(
        total=config.HTTP_RETRIES,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    _local.session = s
    return s


def get(url: str, referer: str | None = None, **kw) -> rq.Response:
    headers = dict(kw.pop("headers", {}))
    if referer:
        headers["Referer"] = referer
    kw.setdefault("timeout", config.HTTP_TIMEOUT)
    resp = session().get(url, headers=headers, **kw)
    resp.raise_for_status()
    return resp


def polite_sleep(seconds: float):
    if seconds > 0:
        time.sleep(seconds)
