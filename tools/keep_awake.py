# -*- coding: utf-8 -*-
"""Streamlit Community Cloud 앱을 깨워 둡니다.

    python tools/keep_awake.py <앱 URL>

왜 curl로는 안 되는가 (측정된 사실):
    GET 요청은 200을 돌려주지만 그건 빈 HTML 껍데기(약 4KB)뿐입니다. 그
    안에는 비어 있는 <div id="root">와 "JavaScript가 필요하다"는 안내만
    있습니다. 파이썬 앱은 **브라우저가 자바스크립트를 실행해 웹소켓
    (/_stcore/stream)을 열 때** 비로소 시작됩니다. /_stcore/health 도
    같은 껍데기로 넘어갑니다. 그래서 진짜 브라우저가 필요합니다.

Community Cloud는 **12시간 동안 접속이 없으면** 앱을 재웁니다. 끄는 설정은
없습니다(공식 문서에도 없음). 그래서 주기적으로 방문해 두는 것 말고는 방법이
없습니다. 비밀번호 잠금이 걸려 있어도 괜찮습니다 — 로그인까지 할 필요 없이
웹소켓이 붙는 순간 앱이 깨어 있는 상태가 됩니다.
"""
import sys

WAKE_TEXTS = ("Yes, get this app back up", "get this app back up",
              "앱을 다시", "back up!")


def main(url: str, timeout_ms: int = 120_000) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        print(f"방문: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # 잠들어 있으면 깨우기 버튼이 뜹니다. 없으면 이미 깨어 있는 것입니다.
        woke = False
        for text in WAKE_TEXTS:
            btn = page.get_by_text(text, exact=False)
            try:
                if btn.count() and btn.first.is_visible():
                    print(f"잠들어 있었습니다 — '{text}' 눌러 깨웁니다")
                    btn.first.click()
                    woke = True
                    break
            except Exception:
                continue

        # 웹소켓이 붙어 앱이 실제로 뜨는지 확인합니다. 이게 붙어야 '방문'으로
        # 인정되고, 안 붙으면 다음 주기에도 또 잠들어 있습니다.
        ok = False
        try:
            page.wait_for_selector('[data-testid="stAppViewContainer"]',
                                   timeout=timeout_ms if woke else 45_000)
            ok = True
        except Exception:
            pass

        # 화면이 실제로 그려졌는지 한 박자 뒤에 글자로 확인합니다. 껍데기만
        # 받은 경우와 앱이 뜬 경우를 로그에서 구분할 수 있어야 합니다.
        page.wait_for_timeout(3_000)
        title = (page.title() or "").strip()
        body = ""
        try:
            body = (page.inner_text("body") or "").strip()
        except Exception:
            pass
        browser.close()

    body = body[:200].replace("\n", " ")

    print(f"제목: {title!r}")
    print(f"화면 앞부분: {body!r}")
    if ok and not body:
        print("::notice::화면 틀은 떴지만 글자가 없습니다 — 비밀번호 잠금이면 "
              "정상입니다(웹소켓이 붙었으므로 접속으로 인정됩니다).")
    if ok:
        print("깨우기 완료" if woke else "이미 깨어 있었습니다")
        return 0
    print("::warning::앱 화면이 끝내 뜨지 않았습니다 — URL이나 앱 상태를 "
          "확인하세요. (다음 주기에 다시 시도합니다)")
    return 0            # 깨우기는 거들기일 뿐이라 실패로 레포를 붉히지 않습니다


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith("http"):
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
