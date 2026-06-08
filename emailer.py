"""Email sender with two backends.

Primary: Gmail API over HTTPS (works on Railway, which blocks outbound SMTP).
Sends as the authenticated Workspace user (e.g. business@viralasia.co) — no
domain DNS verification needed. Configure with:
    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN     (mint once via scripts/gmail_oauth_setup.py)
    EMAIL_FROM              From address (defaults to GMAIL_SENDER / SMTP_USER)

Fallback: SMTP (for local/dev where SMTP isn't blocked). Configure with:
    SMTP_HOST (default smtp.gmail.com), SMTP_PORT (587), SMTP_USER,
    SMTP_PASSWORD / SMTP_PASS (Google App Password).
"""

import base64
import os
import smtplib
import socket
import ssl
from contextlib import contextmanager
from email.message import EmailMessage


@contextmanager
def _prefer_ipv4():
    """Force DNS resolution to IPv4 for the duration of the block.

    Railway containers often lack routable IPv6, so connecting to an AAAA
    address fails with 'Network is unreachable' (errno 101). We keep the
    hostname for SNI/cert verification and only constrain the address family.
    """
    orig = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        res = orig(host, port, socket.AF_INET, type, proto, flags)
        return res or orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = orig


def _cfg() -> tuple[str, int, str, str]:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    user = os.environ.get("SMTP_USER", "")
    pwd  = os.environ.get("SMTP_PASSWORD") or os.environ.get("SMTP_PASS", "")
    return host, port, user, pwd


def _gmail_configured() -> bool:
    return all(os.environ.get(k) for k in
               ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))


def email_configured() -> bool:
    """True if either the Gmail API or SMTP backend is configured."""
    if _gmail_configured():
        return True
    _, _, user, pwd = _cfg()
    return bool(user and pwd)


def _gmail_access_token() -> str:
    import requests
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
            "grant_type":    "refresh_token",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Gmail token refresh failed ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def _send_gmail_api(msg: EmailMessage) -> bool:
    import requests
    token = _gmail_access_token()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {token}"},
        json={"raw": raw},
        timeout=30,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Gmail send failed ({r.status_code}): {r.text[:300]}")
    return True


def send_email(to, subject: str, *, html: str | None = None, text: str | None = None,
               attachments: list[tuple[str, bytes]] | None = None,
               from_addr: str | None = None, from_name: str | None = None) -> bool:
    """Send an email via SMTP.

    to           — address string ("Name <a@b.com>" or "a@b.com") or list of them.
    attachments  — list of (filename, bytes) PDF attachments.
    """
    use_gmail = _gmail_configured()
    host, port, user, pwd = _cfg()
    if not use_gmail and (not user or not pwd):
        raise RuntimeError("Configure the Gmail API (GMAIL_*) or SMTP (SMTP_USER/SMTP_PASS).")

    from_addr = (from_addr or os.environ.get("EMAIL_FROM")
                 or os.environ.get("GMAIL_SENDER") or user)

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

    # Gmail API (HTTPS) — preferred; SMTP is blocked on Railway.
    if use_gmail:
        return _send_gmail_api(msg)

    context = ssl.create_default_context()
    with _prefer_ipv4():
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
