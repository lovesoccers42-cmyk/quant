# -*- coding: utf-8 -*-
"""공용 HTTP 세션.

해외 IP(GitHub Actions 러너)에서 네이버/FnGuide/WISE에 접근할 때
기본 User-Agent로는 차단되는 경우가 많아 브라우저 헤더를 붙입니다.
"""
import threading
import time

import requests as rq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

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
