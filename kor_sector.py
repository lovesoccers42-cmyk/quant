# -*- coding: utf-8 -*-
"""WISE 섹터 구성 종목 수집."""
import pandas as pd

import config
import http_util
import store

SECTOR_CODES = ["G25", "G35", "G50", "G40", "G10",
                "G20", "G55", "G30", "G15", "G45"]

REFERER = "https://www.wiseindex.com/Index"


def collect(biz_day: str) -> dict:
    frames = []
    failed = []

    for code in SECTOR_CODES:
        url = (f"https://www.wiseindex.com/Index/GetIndexComponets"
               f"?ceil_yn=0&dt={biz_day}&sec_cd={code}")
        try:
            data = http_util.get(url, referer=REFERER).json()
            frame = pd.json_normalize(data["list"])
            if frame.empty:
                raise ValueError("빈 응답")
            frames.append(frame)
        except Exception:
            failed.append(code)
        http_util.polite_sleep(1.0)

    if not frames:
        raise RuntimeError(
            "WISE 섹터 데이터를 하나도 받지 못했습니다. "
            "해외 IP 차단 또는 wiseindex.com 구조 변경 가능성."
        )

    df = pd.concat(frames, axis=0)[["IDX_CD", "CMP_CD", "CMP_KOR", "SEC_NM_KOR"]]
    df["기준일"] = pd.to_datetime(biz_day)
    n = store.upsert("kor_sector", df)

    return {"rows": n, "sectors_ok": len(frames), "sectors_failed": failed}


if __name__ == "__main__":
    from kor_ticker import latest_business_day
    print(collect(latest_business_day()))
