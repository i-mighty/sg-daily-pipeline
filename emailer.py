"""Minimal SMTP email sender — works with Gmail SMTP using an App Password.

Env vars:
    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 587 (STARTTLS); use 465 for implicit SSL
    SMTP_USER      the full Gmail address you authenticate as
    SMTP_PASSWORD  a Google "App Password" (16 chars, requires 2-Step Verification)
    EMAIL_FROM     From address (defaults to SMTP_USER)

Gmail note: the From address is normally the authenticated account. To send as a
different address (e.g. business@viralasia.co), add it as a verified "Send mail as"
alias in Gmail settings first; otherwise Gmail rewrites it to SMTP_USER.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage


def _cfg() -> tuple[str, int, str, str]:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    user = os.environ.get("SMTP_USER", "")
    pwd  = os.environ.get("SMTP_PASSWORD") or os.environ.get("SMTP_PASS", "")
    return host, port, user, pwd


def email_configured() -> bool:
    """True if SMTP credentials are present."""
    _, _, user, pwd = _cfg()
    return bool(user and pwd)


def send_email(to, subject: str, *, html: str | None = None, text: str | None = None,
               attachments: list[tuple[str, bytes]] | None = None,
               from_addr: str | None = None, from_name: str | None = None) -> bool:
    """Send an email via SMTP.

    to           — address string ("Name <a@b.com>" or "a@b.com") or list of them.
    attachments  — list of (filename, bytes) PDF attachments.
    """
    host, port, user, pwd = _cfg()
    if not user or not pwd:
        raise RuntimeError("SMTP_USER and SMTP_PASSWORD must be set.")

    from_addr = from_addr or os.environ.get("EMAIL_FROM") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    to_list = [to] if isinstance(to, str) else list(to)
    msg["To"] = ", ".join(to_list)

    msg.set_content(text or "This message requires an HTML-capable email client.")
    if html:
        msg.add_alternative(html, subtype="html")

    for filename, content in (attachments or []):
        msg.add_attachment(content, maintype="application", subtype="pdf", filename=filename)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)
    return True
