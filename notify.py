# -*- coding: utf-8 -*-
"""리포트 발송 — 이메일(첨부 지원) / 텔레그램. 설정이 비면 해당 채널은 건너뜁니다."""
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests as rq

import config


def send_email(subject: str, body: str, attachments: list | None = None) -> bool:
    if not (config.SMTP_USER and config.SMTP_PASS and config.REPORT_EMAIL):
        return False

    msg = EmailMessage()
    msg["From"] = config.SMTP_USER
    msg["To"] = config.REPORT_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)

    for path in attachments or []:
        p = Path(path)
        if not p.exists():
            continue
        # 25MB 제한을 고려해 큰 파일은 건너뜀
        if p.stat().st_size > 20 * 1024 * 1024:
            continue
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype,
                           subtype=subtype, filename=p.name)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as server:
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASS)
        server.send_message(msg)
    return True


def send_telegram(text: str) -> bool:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 4000):
        rq.post(url, data={"chat_id": config.TELEGRAM_CHAT_ID,
                           "text": text[i:i + 4000]}, timeout=20)
    return True


def send_report(subject: str, body: str, attachments: list | None = None) -> list:
    """설정된 모든 채널로 발송. 성공한 채널 목록 반환."""
    sent = []
    try:
        if send_email(subject, body, attachments):
            sent.append("email")
    except Exception as e:
        print(f"[notify] 이메일 발송 실패: {e}")
    try:
        if send_telegram(f"{subject}\n\n{body}"):
            sent.append("telegram")
    except Exception as e:
        print(f"[notify] 텔레그램 발송 실패: {e}")
    return sent
