# -*- coding: utf-8 -*-
"""WISE 섹터 구성 종목 수집."""
import logging
from collections import Counter

import pandas as pd

import config
import http_util
import store

SECTOR_CODES = ["G25", "G35", "G50", "G40", "G10",
                "G20", "G55", "G30", "G15", "G45"]

REFERER = "https://www.wiseindex.com/Index"

log = logging.getLogger("quant_agent.sector")


def collect(biz_day: str) -> dict:
    frames = []
    failed = []
    reasons = Counter()

    for code in SECTOR_CODES:
        url = (f"https://www.wiseindex.com/Index/GetIndexComponets"
               f"?ceil_yn=0&dt={biz_day}&sec_cd={code}")
        try:
            resp = http_util.get(url, referer=REFERER)
            try:
                data = resp.json()
            except ValueError:
                # 무엇이 왔는지 남깁니다 — 차단 페이지인지 구조 변경인지 갈립니다
                body = " ".join(resp.text.split())[:100] or "(본문없음)"
                raise ValueError(f"JSON 아님 | HTTP {resp.status_code} | 본문[{body}]")
            frame = pd.json_normalize(data["list"])
            if frame.empty:
                raise ValueError("빈 응답(list 0건)")
            frames.append(frame)
        except Exception as e:
            failed.append(code)
            reasons[f"{type(e).__name__}: {str(e)[:120]}"] += 1
        http_util.polite_sleep(1.0)

    if not frames:
        # 섹터는 한 달에 거의 안 바뀝니다. 이미 받아둔 게 있으면 그걸 계속 쓰고
        # 월간 실행 전체를 실패시키지 않습니다 — 주가·재무 수집이 더 중요합니다.
        existing = store.read("kor_sector")
        if len(existing):
            last = pd.to_datetime(existing["기준일"]).max()
            age = (pd.to_datetime(biz_day) - last).days
            log.warning("WISE 수집 실패 — 기존 섹터(%s, %d일 전)를 그대로 씁니다. 사유: %s",
                        last.date(), age, dict(reasons.most_common(3)))
            return {"rows": 0, "sectors_ok": 0, "sectors_failed": failed,
                    "fail_reasons": dict(reasons.most_common(3)),
                    "기존섹터사용": f"{last:%Y-%m-%d} ({age}일 전)",
                    "종목수": int(existing[existing["기준일"] == last].shape[0])}
        raise RuntimeError(
            "WISE 섹터 데이터를 하나도 받지 못했고 기존 데이터도 없습니다. "
            f"해외 IP 차단 또는 wiseindex.com 구조 변경 가능성. "
            f"사유: {dict(reasons.most_common(3))}")

    df = pd.concat(frames, axis=0)[["IDX_CD", "CMP_CD", "CMP_KOR", "SEC_NM_KOR"]]
    df["기준일"] = pd.to_datetime(biz_day)
    n = store.upsert("kor_sector", df)

    return {"rows": n, "sectors_ok": len(frames), "sectors_failed": failed}


if __name__ == "__main__":
    from kor_ticker import latest_business_day
    print(collect(latest_business_day()))
