"""Send a course PDF (or a build report) by email over SMTP. Settings come from .env:

  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587                 (465 means SSL from the start; anything else uses STARTTLS)
  SMTP_USER=you@gmail.com
  SMTP_PASSWORD=app-password    (for Gmail: an App Password, not the account password)
  SMTP_FROM=you@gmail.com       (optional; defaults to SMTP_USER)
"""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_BYTES = 20 * 1024 * 1024    # Gmail refuses messages over 25 MB once encoded


class NotConfigured(Exception):
    pass


def settings() -> dict:
    s = {k: os.environ.get(f"SMTP_{k.upper()}", "").strip() for k in ("host", "port", "user", "password", "from")}
    if not (s["host"] and s["user"] and s["password"]):
        raise NotConfigured("Email isn't set up: add SMTP_HOST, SMTP_USER and SMTP_PASSWORD to .env")
    s["port"] = int(s["port"] or 587)
    s["from"] = s["from"] or s["user"]
    return s


def send_pdf(to: str, pdf: Path, title: str, smtp=smtplib) -> None:
    send_file(to, pdf, f"Teach Me course: {title}", f"Here is your Teach Me course \"{title}\" as a PDF.\n",
              ("application", "pdf"), smtp)


def send_file(to: str, path: Path, subject: str, body: str, mime: tuple[str, str], smtp=smtplib) -> None:
    """Email one file as an attachment. Raises NotConfigured without SMTP settings."""
    to = (to or "").strip()
    if not EMAIL.match(to):
        raise ValueError(f"Not an email address: {to!r}")
    path = Path(path)
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"{path.name} is {len(data) // (1024 * 1024)} MB, too big to email")
    s = settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["from"]
    msg["To"] = to
    msg.set_content(body)
    msg.add_attachment(data, maintype=mime[0], subtype=mime[1], filename=path.name)
    if s["port"] == 465:
        server = smtp.SMTP_SSL(s["host"], s["port"], timeout=60)
    else:
        server = smtp.SMTP(s["host"], s["port"], timeout=60)
        server.starttls()
    with server:
        server.login(s["user"], s["password"])
        server.send_message(msg)
